"""gas-gauge: the status line that writes the session's cost to disk."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gas_gauge as usage_sidecar

SID = "abcdef12-3456-7890-abcd-ef1234567890"


def payload(**over):
    base = {
        "session_id": SID,
        "transcript_path": "/home/x/.claude/projects/-home-x-gt/" + SID + ".jsonl",
        "version": "2.1.299",
        "model": {"id": "claude-fable-5-1", "display_name": "Fable"},
        "workspace": {"current_dir": "/home/x/gt", "project_dir": "/home/x/gt"},
        "cost": {"total_cost_usd": 12.3456, "total_duration_ms": 1000},
        "context_window": {
            "total_input_tokens": 500000,
            "total_output_tokens": 4000,
            "used_percentage": 42.0,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 23.4, "resets_at": 1760000000},
            "seven_day": {"used_percentage": 61.0, "resets_at": 1760400000},
        },
    }
    base.update(over)
    return base


def run(stdin, home, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV, usage_sidecar.ROOT_ENV,
                         "CLAUDE_CONFIG_DIR")}
    env["HOME"] = str(home)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "gas_gauge"],
        check=False,
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestWrites:
    def test_writes_the_record_and_prints_a_line(self, tmp_path):
        result = run(json.dumps(payload()), tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "gt · $12.35 · 5h 23% · 7d 61% · ctx 42% · fable-5-1"
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["session_id"] == SID
        assert record["total_cost_usd"] == 12.3456
        assert record["total_input_tokens"] == 500000
        assert record["rate_limits"]["five_hour"]["used_percentage"] == 23.4
        assert record["rate_limits"]["seven_day"]["resets_at"] == 1760400000
        assert record["model"] == "claude-fable-5-1"
        assert record["cwd"] == "/home/x/gt"
        assert isinstance(record["written_at"], float)

    def test_env_override_moves_the_folder(self, tmp_path):
        target = tmp_path / "elsewhere"
        result = run(
            json.dumps(payload()),
            tmp_path,
            {usage_sidecar.SIDECAR_DIR_ENV: str(target)},
        )
        assert result.returncode == 0
        assert (target / f"{SID}.json").exists()
        assert not (tmp_path / ".claude").exists()

    def test_a_later_run_overwrites_the_earlier_one(self, tmp_path):
        run(json.dumps(payload(cost={"total_cost_usd": 1.0})), tmp_path)
        run(json.dumps(payload(cost={"total_cost_usd": 2.5})), tmp_path)
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["total_cost_usd"] == 2.5
        leftovers = [
            p
            for p in (tmp_path / ".claude" / "usage-sidecar").iterdir()
            if p.name.startswith(".tmp-")
        ]
        assert leftovers == []

    def test_before_the_first_api_response_cost_is_null_and_still_recorded(
        self, tmp_path
    ):
        result = run(json.dumps(payload(cost={}, rate_limits={})), tmp_path)
        assert result.returncode == 0
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["total_cost_usd"] is None
        assert record["peak_total_cost_usd"] is None
        # The line still prints what it has; a reader treats a null cost as "no price yet".
        assert "fable-5-1" in result.stdout

    def test_a_null_cost_after_a_resume_does_not_erase_the_price(self, tmp_path):
        """Review 2026-10-14: the first status-line call after a resume carries null."""
        run(json.dumps(payload(cost={"total_cost_usd": 6.0})), tmp_path)
        run(json.dumps(payload(cost={"total_cost_usd": None})), tmp_path)
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["total_cost_usd"] == 6.0
        assert record["peak_total_cost_usd"] == 6.0

    def test_a_resumed_session_keeps_its_peak(self, tmp_path):
        run(json.dumps(payload(cost={"total_cost_usd": 6.0})), tmp_path)
        run(
            json.dumps(payload(cost={"total_cost_usd": 0.4})), tmp_path
        )  # restarted from zero
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["total_cost_usd"] == 0.4
        assert record["peak_total_cost_usd"] == 6.0
        # a reader prices the session at the higher of the two
        assert max(record["total_cost_usd"], record["peak_total_cost_usd"]) == 6.0

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e400"])
    def test_non_finite_numbers_are_not_numbers(self, tmp_path, bad):
        raw = json.dumps(payload()).replace("12.3456", bad).replace("500000", bad)
        result = run(raw, tmp_path)
        assert result.returncode == 0
        record = json.loads(
            (tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text()
        )
        assert record["total_cost_usd"] is None
        assert record["total_input_tokens"] is None


class TestNeverFails:
    @pytest.mark.parametrize(
        "stdin", ["", "not json", "[]", "null", '{"cost": {"total_cost_usd": 1}}']
    )
    def test_bad_or_sessionless_input_exits_zero_and_writes_nothing(
        self, tmp_path, stdin
    ):
        result = run(stdin, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (tmp_path / ".claude").exists()

    @pytest.mark.parametrize("sid", ["../escape", "a/b", "", "x" * 200, 42])
    def test_a_session_id_that_is_not_a_filename_is_refused(self, tmp_path, sid):
        result = run(json.dumps(payload(session_id=sid)), tmp_path)
        assert result.returncode == 0
        assert not (tmp_path / ".claude").exists()

    def test_booleans_and_strings_are_not_numbers(self):
        record = usage_sidecar.record_from(
            payload(
                cost={"total_cost_usd": True},
                context_window={"total_input_tokens": "9"},
            )
        )
        assert record["total_cost_usd"] is None
        assert record["total_input_tokens"] is None


class TestConfigurable:
    def test_default_line(self):
        assert usage_sidecar.status_line(usage_sidecar.record_from(payload()), color=False) == "gt · $12.35 · 5h 23% · 7d 61% · ctx 42% · fable-5-1"

    def test_segments_in_the_order_given_with_a_separator(self):
        rec = usage_sidecar.record_from(payload())
        assert usage_sidecar.status_line(rec, ["model", "cost", "tokens"], " | ", color=False) == "fable-5-1 | $12.35 | 500k in / 4k out"

    def test_unknown_segment_names_are_ignored_and_empty_segments_skipped(self):
        rec = usage_sidecar.record_from(payload(rate_limits={}))
        assert usage_sidecar.status_line(rec, ["5h", "nonsense", "cost"], color=False) == "$12.35"

    def test_flags_reach_the_line_and_no_file_writes_nothing(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV)}
        env["HOME"] = str(tmp_path)
        r = subprocess.run([sys.executable, "-m", "gas_gauge", "--segments", "cost,7d", "--separator", " / ", "--no-file", "--no-color"],
                           input=json.dumps(payload()), env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0 and r.stdout.strip() == "$12.35 / 7d 61%"
        assert not (tmp_path / ".claude").exists()

    def test_dir_flag_moves_the_file(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV)}
        env["HOME"] = str(tmp_path)
        r = subprocess.run([sys.executable, "-m", "gas_gauge", "--dir", str(tmp_path / "g")],
                           input=json.dumps(payload()), env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0 and (tmp_path / "g" / f"{SID}.json").exists()

    def test_a_bad_flag_never_breaks_the_prompt(self):
        r = subprocess.run([sys.executable, "-m", "gas_gauge", "--bogus"], input="{}", capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0


class TestColourByPace:
    """Dull yellow when a window is being spent faster than time passes; red when nearly empty."""
    Y, R, X = usage_sidecar.YELLOW, usage_sidecar.RED, usage_sidecar.RESET

    def _rec(self, five=None, seven=None, now=1_000_000.0):
        limits = {}
        if five is not None:
            pct, elapsed = five
            limits["five_hour"] = {"used_percentage": pct, "resets_at": now + (1 - elapsed) * 5 * 3600}
        if seven is not None:
            pct, elapsed = seven
            limits["seven_day"] = {"used_percentage": pct, "resets_at": now + (1 - elapsed) * 7 * 86400}
        return usage_sidecar.record_from(payload(rate_limits=limits)), now

    def test_day_one_at_eighteen_percent_is_ahead_of_pace(self):
        rec, now = self._rec(seven=(18, 1 / 7))          # 14% of the week gone, 18% spent
        assert usage_sidecar.status_line(rec, ["7d"], now=now) == f"{self.Y}7d 18%{self.X}"

    def test_on_pace_is_plain(self):
        rec, now = self._rec(seven=(15, 1 / 7))          # within the 5-point margin
        assert usage_sidecar.status_line(rec, ["7d"], now=now) == "7d 15%"
        rec, now = self._rec(seven=(40, 0.5))
        assert usage_sidecar.status_line(rec, ["7d"], now=now) == "7d 40%"

    def test_nearly_empty_is_red_whatever_the_pace(self):
        rec, now = self._rec(five=(92, 0.95))
        assert usage_sidecar.status_line(rec, ["5h"], now=now) == f"{self.R}5h 92%{self.X}"

    def test_five_hour_window_early_and_heavy_is_yellow(self):
        rec, now = self._rec(five=(67, 0.3))             # 30% of the window gone, 67% spent
        assert usage_sidecar.status_line(rec, ["5h"], now=now) == f"{self.Y}5h 67%{self.X}"

    def test_context_colours_by_its_own_thresholds(self):
        rec = usage_sidecar.record_from(payload(context_window={"used_percentage": 72}))
        assert usage_sidecar.status_line(rec, ["context"]) == f"{self.Y}ctx 72%{self.X}"
        rec = usage_sidecar.record_from(payload(context_window={"used_percentage": 90}))
        assert usage_sidecar.status_line(rec, ["ctx"]) == f"{self.R}ctx 90%{self.X}"  # alias

    def test_no_resets_at_means_no_pace_judgement(self):
        rec = usage_sidecar.record_from(payload(rate_limits={"seven_day": {"used_percentage": 60}}))
        assert usage_sidecar.status_line(rec, ["7d"]) == "7d 60%"

    def test_no_color_env_and_flag(self, monkeypatch):
        rec, now = self._rec(seven=(18, 1 / 7))
        assert usage_sidecar.status_line(rec, ["7d"], now=now, color=False) == "7d 18%"
        monkeypatch.setenv("NO_COLOR", "1")
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV)}
        r = subprocess.run([sys.executable, "-m", "gas_gauge", "--no-file"], input=json.dumps(payload()), env=env,
                           capture_output=True, text=True, timeout=30, check=False)
        assert "\033[" not in r.stdout


class TestWhere:
    def test_under_the_root_names_the_first_folder(self, tmp_path):
        folder = tmp_path / "webshop" / "src" / "app"
        folder.mkdir(parents=True)
        assert usage_sidecar.where(str(folder), str(tmp_path)) == "webshop"

    def test_a_worktree_under_the_root_adds_its_name(self, tmp_path):
        wt = tmp_path / "webshop" / ".worktrees" / "fix-login" / "src"
        wt.mkdir(parents=True)
        assert usage_sidecar.where(str(wt), str(tmp_path)) == "webshop/fix-login"

    def test_the_root_itself_is_its_own_name(self, tmp_path):
        assert usage_sidecar.where(str(tmp_path), str(tmp_path)) == tmp_path.name

    def test_outside_the_root_names_the_git_repo(self, tmp_path):
        repo = tmp_path / "elsewhere" / "myrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "pkg").mkdir(parents=True)
        root = tmp_path / "gt"
        root.mkdir()
        assert usage_sidecar.where(str(repo / "src" / "pkg"), str(root)) == "myrepo"
        assert usage_sidecar.where(str(repo / "src" / "pkg")) == "myrepo"

    def test_no_repo_falls_back_to_the_folder(self, tmp_path):
        plain = tmp_path / "notes"
        plain.mkdir()
        assert usage_sidecar.where(str(plain)) == "notes"

    def test_no_cwd_draws_nothing(self):
        assert usage_sidecar.where(None) is None
        assert usage_sidecar.where("") is None

    def test_root_flag_and_env_reach_the_line(self, tmp_path):
        folder = tmp_path / "blog" / "src"
        folder.mkdir(parents=True)
        data = json.dumps(payload(workspace={"current_dir": str(folder)}))
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV, usage_sidecar.ROOT_ENV)}
        cmd = [sys.executable, "-m", "gas_gauge", "--no-file", "--no-color", "--segments", "where,cost"]
        r = subprocess.run(cmd + ["--root", str(tmp_path)], input=data, env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.stdout.strip() == "blog · $12.35"
        r = subprocess.run(cmd, input=data, env={**env, usage_sidecar.ROOT_ENV: str(tmp_path)}, capture_output=True, text=True, timeout=30, check=False)
        assert r.stdout.strip() == "blog · $12.35"


def test_where_sees_through_a_symlinked_root(tmp_path):
    real = tmp_path / "data" / "code"
    (real / "webshop" / "src").mkdir(parents=True)
    link = tmp_path / "code"
    link.symlink_to(real)
    assert usage_sidecar.where(str(real / "webshop" / "src"), str(link)) == "webshop"
    assert usage_sidecar.where(str(link / "webshop" / "src"), str(real)) == "webshop"


ENDPOINT_BODY = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-10-17T03:00:00.129588+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 3, "resets_at": "2026-10-17T03:00:00+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 89, "resets_at": "2026-10-21T14:00:00+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 83, "resets_at": "2026-10-21T14:00:00.129732+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 5, "resets_at": None,
         "scope": {"model": None, "surface": {"display_name": "Cowork"}}},
    ],
    "weekly_scoped_shares": [{"limit_index": 2, "used_percent_of_weekly": 42, "allowance_percent_of_weekly": 50}],
}
FABLE_RESET = 1792591200.129732  # 2026-10-21T14:00:00.129732Z


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def creds(home, expires_ms, token="tok-123"):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires_ms}}))


class TestModelWeek:
    """The answering model's own weekly window, read from the usage endpoint (--usage-api)."""

    def test_the_endpoint_body_becomes_the_model_windows(self):
        scoped = usage_sidecar.scoped_windows(ENDPOINT_BODY)
        assert scoped == {"fable": {"used_percentage": 83, "resets_at": pytest.approx(FABLE_RESET),
                                    "share_of_weekly": 42, "allowance_of_weekly": 50}}

    @pytest.mark.parametrize("body", [None, [], {}, {"limits": "x"}, {"limits": [None, 3, {"kind": "weekly_scoped"}]}])
    def test_a_strange_body_is_no_windows(self, body):
        assert usage_sidecar.scoped_windows(body) == {}

    def test_shows_for_the_model_that_has_one(self):
        rec = usage_sidecar.record_from(payload())
        rec["model_scoped"] = usage_sidecar.scoped_windows(ENDPOINT_BODY)
        now = FABLE_RESET - 5 * 86400  # two days into the week, 83% gone: ahead of pace
        assert usage_sidecar.status_line(rec, ["model"], now=now, color=False) == "fable-5-1 83%"
        assert usage_sidecar.status_line(rec, ["model"], now=now) == f"fable-5-1 {usage_sidecar.YELLOW}83%{usage_sidecar.RESET}"

    def test_nothing_for_a_model_without_one(self):
        rec = usage_sidecar.record_from(payload(model={"id": "claude-opus-5-5"}))
        rec["model_scoped"] = usage_sidecar.scoped_windows(ENDPOINT_BODY)
        assert usage_sidecar.status_line(rec, ["7d", "model"], color=False) == "7d 61% · opus-5-5"

    def test_in_the_default_line_by_the_model_name_once(self):
        rec = usage_sidecar.record_from(payload())
        rec["model_scoped"] = {"fable": {"used_percentage": 30, "resets_at": None}}
        assert usage_sidecar.status_line(rec, color=False) == "gt · $12.35 · 5h 23% · 7d 61% · ctx 42% · fable-5-1 30%"

    def test_the_token_is_used_only_while_valid(self, tmp_path):
        now = 1_800_000_000.0
        assert usage_sidecar.login_token(env={}, home=tmp_path, now=now) is None
        creds(tmp_path, (now + 3600) * 1000)
        assert usage_sidecar.login_token(env={}, home=tmp_path, now=now) == "tok-123"
        creds(tmp_path, (now + 30) * 1000)  # expires inside the margin: leave it to Claude Code
        assert usage_sidecar.login_token(env={}, home=tmp_path, now=now) is None
        moved = tmp_path / "cfg"
        moved.mkdir()
        (moved / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "m", "expiresAt": (now + 3600) * 1000}}))
        assert usage_sidecar.login_token(env={"CLAUDE_CONFIG_DIR": str(moved)}, home=tmp_path, now=now) == "m"

    def test_a_fetch_writes_the_reading_with_the_token(self, tmp_path):
        seen = {}

        def opener(request, timeout):
            seen["auth"], seen["url"], seen["timeout"] = request.get_header("Authorization"), request.full_url, timeout
            return FakeResponse(ENDPOINT_BODY)

        usage_sidecar.fetch_usage(tmp_path, now=100.0, token="tok-123", opener=opener)
        assert seen == {"auth": "Bearer tok-123", "url": usage_sidecar.USAGE_URL, "timeout": usage_sidecar.USAGE_TIMEOUT}
        usage = usage_sidecar.read_usage(tmp_path)
        assert usage["fetched_at"] == 100.0 and usage["ok_at"] == 100.0
        assert usage["model_scoped"]["fable"]["used_percentage"] == 83

    def test_a_failed_fetch_keeps_the_last_good_reading_until_it_goes_stale(self, tmp_path):
        usage_sidecar.fetch_usage(tmp_path, now=100.0, token="t", opener=lambda r, timeout: FakeResponse(ENDPOINT_BODY))

        def broken(request, timeout):
            raise OSError("down")

        usage_sidecar.fetch_usage(tmp_path, now=500.0, token="t", opener=broken)
        usage = usage_sidecar.read_usage(tmp_path)
        assert usage["error"] == "OSError" and usage["fetched_at"] == 500.0 and usage["ok_at"] == 100.0
        assert usage_sidecar.fresh_scoped(usage, 600.0)["fable"]["used_percentage"] == 83
        assert usage_sidecar.fresh_scoped(usage, 100.0 + usage_sidecar.USAGE_STALE + 1) is None
        usage_sidecar.fetch_usage(tmp_path, now=700.0, token=None, opener=broken)  # no token: no request
        assert usage_sidecar.read_usage(tmp_path)["error"] == "no valid login token"

    def test_one_background_fetch_per_interval_whoever_draws(self, tmp_path):
        spawned = []
        refresh = lambda now: usage_sidecar.refresh_usage_in_background(tmp_path, now, spawn=spawned.append)
        now = time.time()
        assert refresh(now) is True                          # nothing on file
        assert refresh(now) is False                         # a fetch holds the lock
        (tmp_path / usage_sidecar.USAGE_LOCK).unlink()       # the child finished...
        usage_sidecar.fetch_usage(tmp_path, now=now, token=None)
        assert refresh(now + 10) is False                    # ...and its reading is fresh
        assert refresh(now + usage_sidecar.USAGE_TTL + 1) is True
        assert len(spawned) == 2

    def test_a_dead_fetch_does_not_hold_the_lock_forever(self, tmp_path):
        lock = tmp_path / usage_sidecar.USAGE_LOCK
        lock.touch()
        old = time.time() - usage_sidecar.LOCK_STALE - 5
        os.utime(lock, (old, old))
        assert usage_sidecar.refresh_usage_in_background(tmp_path, spawn=lambda root: None) is True

    def test_the_flag_draws_the_segment_and_keeps_it_in_the_file(self, tmp_path):
        root = tmp_path / ".claude" / "usage-sidecar"
        root.mkdir(parents=True)
        now = time.time()
        (root / usage_sidecar.USAGE_FILE).write_text(json.dumps(
            {"fetched_at": now, "ok_at": now, "model_scoped": {"fable": {"used_percentage": 83, "resets_at": None}}}))
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV)}
        env["HOME"] = str(tmp_path)
        cmd = [sys.executable, "-m", "gas_gauge", "--no-color", "--segments", "7d,model"]
        r = subprocess.run(cmd + ["--usage-api"], input=json.dumps(payload()), env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0 and r.stdout.strip() == "7d 61% · fable-5-1 83%"
        assert json.loads((root / f"{SID}.json").read_text())["model_scoped"]["fable"]["used_percentage"] == 83
        assert not (root / usage_sidecar.USAGE_LOCK).exists()  # fresh: no fetch started
        r = subprocess.run(cmd, input=json.dumps(payload()), env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.stdout.strip() == "7d 61% · fable-5-1"         # without the flag, nothing is read

    def test_the_child_without_a_login_writes_an_error_and_frees_the_lock(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV, "CLAUDE_CONFIG_DIR")}
        env["HOME"] = str(tmp_path)
        root = tmp_path / "g"
        root.mkdir()
        (root / usage_sidecar.USAGE_LOCK).touch()
        r = subprocess.run([sys.executable, "-m", "gas_gauge", "--usage-api-fetch", "--dir", str(root)],
                           input="", env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0
        assert usage_sidecar.read_usage(root)["error"] == "no valid login token"
        assert not (root / usage_sidecar.USAGE_LOCK).exists()


# -- the plan, and whether it fits ------------------------------------------

WEEK = 7 * 86400


def with_plan(home, name="Ultra"):
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"planDisplayName": name}}))


def week_payload(pct, resets_at, **limits):
    rl = {"seven_day": {"used_percentage": pct, "resets_at": resets_at}}
    rl.update(limits)
    return json.dumps(payload(rate_limits=rl))


class TestPlan:
    def test_the_plan_name_ends_the_default_line(self, tmp_path):
        with_plan(tmp_path)
        result = run(json.dumps(payload()), tmp_path, {"NO_COLOR": "1"})
        assert result.stdout.strip() == "gt · $12.35 · 5h 23% · 7d 61% · ctx 42% · fable-5-1 · Ultra"

    def test_no_config_means_no_segment(self, tmp_path):
        result = run(json.dumps(payload()), tmp_path, {"NO_COLOR": "1"})
        assert not result.stdout.strip().endswith("Ultra")
        assert result.stdout.strip().endswith("fable-5-1")

    def test_config_dir_override_is_read(self, tmp_path):
        moved = tmp_path / "cfg"
        moved.mkdir()
        with_plan(moved, "Pro")
        result = run(json.dumps(payload()), tmp_path, {"NO_COLOR": "1", "CLAUDE_CONFIG_DIR": str(moved)})
        assert result.stdout.strip().endswith("· Pro")

    def test_a_malformed_config_still_draws_the_line(self, tmp_path):
        (tmp_path / ".claude.json").write_text("{not json")
        result = run(json.dumps(payload()), tmp_path, {"NO_COLOR": "1"})
        assert result.returncode == 0
        assert result.stdout.strip().endswith("fable-5-1")

    def test_the_session_file_names_the_plan_but_not_the_weeks(self, tmp_path):
        with_plan(tmp_path)
        run(json.dumps(payload()), tmp_path)
        record = json.loads((tmp_path / ".claude" / "usage-sidecar" / f"{SID}.json").read_text())
        assert record["plan"] == "Ultra"
        assert "plan_weeks" not in record

    def test_four_short_weeks_say_a_smaller_plan_would_do(self, tmp_path):
        with_plan(tmp_path)
        t0 = 1760400000
        for i, pct in enumerate([10, 20, 15, 24]):
            run(week_payload(pct, t0 + i * WEEK), tmp_path)
        assert run(week_payload(1, t0 + 4 * WEEK), tmp_path, {"NO_COLOR": "1"}).stdout.strip().endswith("Ultra \u2193")

    def test_three_closed_weeks_are_too_few_to_say(self, tmp_path):
        with_plan(tmp_path)
        t0 = 1760400000
        for i, pct in enumerate([10, 20, 15]):
            run(week_payload(pct, t0 + i * WEEK), tmp_path)
        assert run(week_payload(1, t0 + 3 * WEEK), tmp_path, {"NO_COLOR": "1"}).stdout.strip().endswith("· Ultra")

    def test_a_week_that_ended_at_93_percent_says_the_plan_ran_short(self, tmp_path):
        with_plan(tmp_path)
        t0 = 1760400000
        run(week_payload(40, t0), tmp_path)
        run(week_payload(93, t0), tmp_path)           # the same week, later and fuller
        out = run(week_payload(5, t0 + WEEK), tmp_path).stdout.strip()
        assert out.endswith(usage_sidecar.RED + "Ultra \u2191" + usage_sidecar.RESET)

    def test_extra_usage_says_the_plan_ran_short_at_once(self, tmp_path):
        with_plan(tmp_path)
        out = run(week_payload(50, 1760400000, spend_limit={"used_percentage": 3, "resets_at": 1760400000}),
                  tmp_path, {"NO_COLOR": "1"}).stdout.strip()
        assert out.endswith("Ultra \u2191")

    def test_a_week_closes_at_its_peak_not_its_last_reading(self, tmp_path):
        root = usage_sidecar.read_weeks(tmp_path)
        for pct in (30, 80, 50):                    # a stale session reports a lower figure later
            root, _ = usage_sidecar.update_weeks(root, {"used_percentage": pct, "resets_at": 100 * WEEK})
        root, _ = usage_sidecar.update_weeks(root, {"used_percentage": 2, "resets_at": 101 * WEEK})
        assert [w["peak"] for w in root["closed"]] == [80]
        assert root["current"]["peak"] == 2

    def test_an_older_reset_from_a_stale_session_changes_nothing(self):
        weeks = {"current": {"resets_at": 101 * WEEK, "peak": 10}, "closed": []}
        after, changed = usage_sidecar.update_weeks(weeks, {"used_percentage": 99, "resets_at": 100 * WEEK})
        assert not changed and after == weeks

    def test_a_reset_that_wobbles_by_seconds_is_the_same_week(self):
        weeks = {"current": {"resets_at": 100 * WEEK, "peak": 10}, "closed": []}
        after, _ = usage_sidecar.update_weeks(weeks, {"used_percentage": 20, "resets_at": 100 * WEEK + 1})
        assert after["closed"] == [] and after["current"]["peak"] == 20

    def test_the_same_week_tolerance_ends_at_an_hour(self):
        weeks = {"current": {"resets_at": 100 * WEEK, "peak": 10}, "closed": []}
        same, _ = usage_sidecar.update_weeks(weeks, {"used_percentage": 20, "resets_at": 100 * WEEK + 3599})
        assert same["closed"] == []
        rolled, _ = usage_sidecar.update_weeks(weeks, {"used_percentage": 20, "resets_at": 100 * WEEK + 3600})
        assert [w["peak"] for w in rolled["closed"]] == [10]

    def test_an_old_heavy_week_no_longer_counts(self, tmp_path):
        with_plan(tmp_path)
        t0 = 1760400000
        run(week_payload(95, t0), tmp_path)
        out = run(week_payload(5, t0 + 10 * WEEK), tmp_path, {"NO_COLOR": "1"}).stdout.strip()
        assert out.endswith("· Ultra")

    def test_light_weeks_spread_over_months_are_not_four_recent_weeks(self, tmp_path):
        with_plan(tmp_path)
        t0 = 1760400000
        for i in range(5):
            run(week_payload(10, t0 + i * 8 * WEEK), tmp_path)    # a week used every two months
        out = run(week_payload(1, t0 + 41 * WEEK), tmp_path, {"NO_COLOR": "1"}).stdout.strip()
        assert out.endswith("· Ultra")

    def test_a_failed_history_write_still_shows_the_verdict(self, tmp_path, monkeypatch, capsys):
        with_plan(tmp_path)
        folder = tmp_path / "g"
        folder.mkdir()
        t0 = 1760400000
        (folder / usage_sidecar.WEEKS_FILE).write_text(json.dumps(
            {"current": {"resets_at": t0, "peak": 95}, "closed": []}))
        real = usage_sidecar._write_json

        def failing(target, obj):
            if target.name == usage_sidecar.WEEKS_FILE:
                raise OSError("disk full")
            return real(target, obj)

        monkeypatch.setattr(usage_sidecar, "_write_json", failing)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(week_payload(5, t0 + WEEK)))
        assert usage_sidecar.main(["--dir", str(folder), "--no-color"]) == 0
        assert capsys.readouterr().out.strip().endswith("Ultra \u2191")

    def test_only_the_last_twelve_weeks_are_kept(self):
        weeks = {"current": None, "closed": []}
        for i in range(20):
            weeks, _ = usage_sidecar.update_weeks(weeks, {"used_percentage": i, "resets_at": (100 + i) * WEEK})
        assert len(weeks["closed"]) == usage_sidecar.WEEKS_KEPT
        assert weeks["closed"][-1]["peak"] == 18

    def test_no_file_writes_no_history(self, tmp_path):
        with_plan(tmp_path)
        proc = subprocess.run([sys.executable, "-m", "gas_gauge", "--no-file"], input=json.dumps(payload()),
                              env={**{k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"},
                                   "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0
        assert not (tmp_path / ".claude" / "usage-sidecar" / usage_sidecar.WEEKS_FILE).exists()

    def test_a_line_without_the_plan_segment_keeps_no_history(self, tmp_path):
        with_plan(tmp_path)
        proc = subprocess.run([sys.executable, "-m", "gas_gauge", "--segments", "cost"], input=json.dumps(payload()),
                              env={**{k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"},
                                   "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=30)
        assert proc.stdout.strip() == "$12.35"
        assert not (tmp_path / ".claude" / "usage-sidecar" / usage_sidecar.WEEKS_FILE).exists()
