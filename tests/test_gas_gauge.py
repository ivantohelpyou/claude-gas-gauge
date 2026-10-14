"""gas-gauge: the status line that writes the session's cost to disk."""

import json
import os
import subprocess
import sys
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
    env = {k: v for k, v in os.environ.items() if k not in (usage_sidecar.SIDECAR_DIR_ENV, usage_sidecar.LEGACY_DIR_ENV)}
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
        assert result.stdout.strip() == "$12.35 · 5h 23% · 7d 61% · context 42% · fable-5-1"
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
        assert usage_sidecar.status_line(usage_sidecar.record_from(payload()), color=False) == "$12.35 · 5h 23% · 7d 61% · context 42% · fable-5-1"

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
        assert usage_sidecar.status_line(rec, ["context"]) == f"{self.Y}context 72%{self.X}"
        rec = usage_sidecar.record_from(payload(context_window={"used_percentage": 90}))
        assert usage_sidecar.status_line(rec, ["ctx"]) == f"{self.R}context 90%{self.X}"  # alias

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
