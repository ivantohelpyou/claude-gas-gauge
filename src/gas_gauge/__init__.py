"""
gas-gauge — a Claude Code status line that shows what the session costs and writes it to disk.

Claude Code runs the statusLine command after every assistant message and pipes it a JSON
object (docs: https://code.claude.com/docs/en/statusline). This prints one line:

    webshop · $13.90 · 5h 17% · 7d 4% · ctx 42% · fable-5-1

and writes the same numbers to ~/.claude/usage-sidecar/<session_id>.json ($GAS_GAUGE_DIR or
$COST_SIDECAR_DIR overrides the folder), so your own tooling can read a session's cost without
anyone pasting /usage.

Rules of the road for a status line: never fail, never block, never write outside its folder.
Bad or missing input prints nothing and exits 0.

Known limits, both shared with /usage itself: the total resets on /clear (a new session id, so
a new file) and restarts from zero when a session is resumed (same id; the file keeps the
earlier peak as peak_total_cost_usd).

The per-model weekly window (Fable's, say) is not in the payload. With --usage-api the gauge
reads it from the endpoint /usage itself reads, in a detached child at most every five minutes,
so drawing the line never waits on the network (see refresh_usage_in_background).
"""


import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SIDECAR_DIR_ENV = "GAS_GAUGE_DIR"
LEGACY_DIR_ENV = "COST_SIDECAR_DIR"  # the name the author's own ledger used first
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS = ("five_hour", "seven_day", "spend_limit")


def sidecar_root(env=None, home=None):
    """Where the files go: $GAS_GAUGE_DIR (or $COST_SIDECAR_DIR), else ~/.claude/usage-sidecar."""
    env = os.environ if env is None else env
    override = env.get(SIDECAR_DIR_ENV) or env.get(LEGACY_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return (Path.home() if home is None else Path(home)) / ".claude" / "usage-sidecar"


def _number(value):
    """A finite number, or None. JSON admits NaN and Infinity; a price of nan would
    pass every comparison downstream (review 2026-10-14)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _previous_cost(target):
    """What the last run wrote for this session: (total, peak), numbers or None."""
    try:
        with open(target, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(old, dict):
        return None, None
    return _number(old.get("total_cost_usd")), _number(old.get("peak_total_cost_usd"))


def record_from(data):
    """The subset of the status-line payload worth keeping, or None if there is
    no usable session id."""
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        return None
    cost = data.get("cost") or {}
    window = data.get("context_window") or {}
    model = data.get("model") or {}
    workspace = data.get("workspace") or {}
    limits = {}
    for name in _WINDOWS:
        entry = (data.get("rate_limits") or {}).get(name)
        if isinstance(entry, dict):
            limits[name] = {
                "used_percentage": _number(entry.get("used_percentage")),
                "resets_at": _number(entry.get("resets_at")),
            }
    return {
        "session_id": session_id,
        "transcript_path": data.get("transcript_path"),
        "cwd": workspace.get("current_dir") or data.get("cwd"),
        "model": model.get("id"),
        "total_cost_usd": _number(cost.get("total_cost_usd")),
        "total_input_tokens": _number(window.get("total_input_tokens")),
        "total_output_tokens": _number(window.get("total_output_tokens")),
        "context_used_percentage": _number(window.get("used_percentage")),
        "rate_limits": limits,
        "claude_code_version": data.get("version"),
        "written_at": time.time(),
    }


def _write_json(target, obj):
    """Atomic: a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def write_record(record, root):
    """Atomic: a reader never sees a half-written file.

    Two things the payload does to a resumed session, and what this does back:
    the first call after a resume carries `total_cost_usd` null (no API response
    yet), so a null never replaces a number already on file; and the total then
    restarts from zero, so `peak_total_cost_usd` keeps the highest figure any
    run wrote; a reader should price the session at the higher of the two.
    """
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{record['session_id']}.json"
    previous_total, previous_peak = _previous_cost(target)
    if record.get("total_cost_usd") is None:
        record["total_cost_usd"] = previous_total
    candidates = [c for c in (record["total_cost_usd"], previous_total, previous_peak) if c is not None]
    record["peak_total_cost_usd"] = max(candidates) if candidates else None
    return _write_json(target, record)


# The per-model weekly windows. Claude Code reads them from this endpoint with its own login
# and leaves them out of the status-line payload (2.1.302: `limits[]` rows of kind
# `weekly_scoped`, labelled e.g. "Fable"). The endpoint is undocumented; a change to it shows
# as the segment disappearing, never as a broken line. The login token is used only while it
# is valid and is never refreshed: a refresh rotates the token Claude Code itself holds.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_FILE = ".usage.json"     # a dotfile, so it is never mistaken for a <session_id>.json
USAGE_LOCK = ".usage.lock"
USAGE_TTL = 300                # seconds between fetches, across every session sharing the folder
USAGE_STALE = 1800             # a reading older than this is not shown
USAGE_TIMEOUT = 5
LOCK_STALE = 60                # a fetch that has held the lock this long has died


def _get(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _epoch(value):
    """Epoch seconds from a number or an ISO 8601 string, or None."""
    if _number(value) is not None:
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def scoped_windows(body):
    """{model label: {used_percentage, resets_at, share_of_weekly, allowance_of_weekly}} from
    the endpoint's weekly_scoped rows. The label is the first word of the server's, lower-cased
    ("Fable" -> "fable"), which is how it is matched against the model id."""
    out = {}
    limits = _get(body, "limits")
    if not isinstance(limits, list):
        return out
    shares = {s.get("limit_index"): s for s in (_get(body, "weekly_scoped_shares") or [])
              if isinstance(s, dict)}
    for i, row in enumerate(limits):
        if _get(row, "kind") != "weekly_scoped":
            continue
        name = _get(row, "scope", "model", "display_name")
        if not isinstance(name, str) or not name.split():
            continue
        share = shares.get(i) or {}
        out[name.split()[0].lower()] = {
            "used_percentage": _number(row.get("percent")),
            "resets_at": _epoch(row.get("resets_at")),
            "share_of_weekly": _number(share.get("used_percent_of_weekly")),
            "allowance_of_weekly": _number(share.get("allowance_percent_of_weekly")),
        }
    return out


def login_token(env=None, home=None, now=None):
    """Claude Code's OAuth access token, or None if it is missing or about to expire."""
    env = os.environ if env is None else env
    config = Path(env["CLAUDE_CONFIG_DIR"]).expanduser() if env.get("CLAUDE_CONFIG_DIR") else \
        (Path.home() if home is None else Path(home)) / ".claude"
    try:
        data = json.loads((config / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token, expires = _get(data, "claudeAiOauth", "accessToken"), _number(_get(data, "claudeAiOauth", "expiresAt"))
    now = time.time() if now is None else now
    if not isinstance(token, str) or not token or expires is None or expires / 1000 <= now + 60:
        return None
    return token


def read_usage(root):
    try:
        with open(root / USAGE_FILE, encoding="utf-8") as fh:
            usage = json.load(fh)
    except (OSError, ValueError):
        return None
    return usage if isinstance(usage, dict) else None


def fetch_usage(root, now=None, token=None, opener=None):
    """One fetch, written to <root>/.usage.json. A failure is written too (as `error`, with the
    last good reading kept), so the next fetch waits USAGE_TTL like any other."""
    now = time.time() if now is None else now
    previous = read_usage(root) or {}
    result = {"fetched_at": now}
    if token is None:
        result["error"] = "no valid login token"
    else:
        request = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-gas-gauge",
        })
        try:
            with (opener or urllib.request.urlopen)(request, timeout=USAGE_TIMEOUT) as response:
                result["model_scoped"] = scoped_windows(json.load(response))
            result["ok_at"] = now
        except Exception as e:  # noqa: BLE001 - recorded, never raised
            result["error"] = type(e).__name__
    if "ok_at" not in result and _number(previous.get("ok_at")) is not None:
        result["model_scoped"], result["ok_at"] = previous.get("model_scoped"), previous["ok_at"]
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / USAGE_FILE, result)
    return result


def _spawn_fetch(root):
    subprocess.Popen([sys.executable, "-m", "gas_gauge", "--usage-api-fetch", "--dir", str(root)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def refresh_usage_in_background(root, now=None, spawn=None):
    """Start a detached fetch when the reading is older than USAGE_TTL and no fetch holds the
    lock. Returns whether it started one. Every session draws the line; one fetch serves all."""
    now = time.time() if now is None else now
    fetched = _number((read_usage(root) or {}).get("fetched_at"))
    if fetched is not None and now - fetched < USAGE_TTL:
        return False
    root.mkdir(parents=True, exist_ok=True)
    lock = root / USAGE_LOCK
    try:
        if now - lock.stat().st_mtime < LOCK_STALE:
            return False
        lock.unlink()
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    (spawn or _spawn_fetch)(root)
    return True


def fresh_scoped(usage, now):
    """The reading's model_scoped, if it is recent enough to show."""
    ok_at = _number((usage or {}).get("ok_at"))
    scoped = (usage or {}).get("model_scoped")
    if ok_at is None or now - ok_at > USAGE_STALE or not isinstance(scoped, dict):
        return None
    return scoped


# The plan, and whether it fits. Claude Code does not put the plan in the status-line payload;
# it keeps it in its own config (~/.claude.json, oauthAccount.planDisplayName, e.g. "Ultra"). The
# field is undocumented: if it moves, the segment disappears, never the line.
#
# Whether the plan fits is read from your own weeks, not from any table of plans: the gauge keeps
# the highest seven-day figure it saw in each window (<dir>/.weeks.json) and, once a few weeks have
# closed, says what they show. A week that ended at 90% or more, or any use of extra usage
# (`spend_limit`), means the plan ran short. Every closed week ending under a quarter means you paid
# for at least four times what you used: a quarter, because plan tiers usually step by about four.
WEEKS_FILE = ".weeks.json"
WEEKS_KEPT = 12        # closed weeks remembered
PLAN_WEEKS = 4         # closed weeks the verdict reads
PLAN_TIGHT_AT = 90.0   # a week that ended this full ran short
PLAN_ROOMY_UNDER = 25.0


def plan_name(env=None, home=None):
    """The plan's display name from Claude Code's config, or None."""
    env = os.environ if env is None else env
    if env.get("CLAUDE_CONFIG_DIR"):
        path = Path(env["CLAUDE_CONFIG_DIR"]).expanduser() / ".claude.json"
    else:
        path = (Path.home() if home is None else Path(home)) / ".claude.json"
    try:
        with open(path, encoding="utf-8") as fh:
            name = _get(json.load(fh), "oauthAccount", "planDisplayName")
    except (OSError, ValueError):
        return None
    return name.strip() if isinstance(name, str) and name.strip() else None


def read_weeks(root):
    try:
        with open(root / WEEKS_FILE, encoding="utf-8") as fh:
            weeks = json.load(fh)
    except (OSError, ValueError):
        return {"current": None, "closed": []}
    if not isinstance(weeks, dict):
        return {"current": None, "closed": []}
    closed = [w for w in weeks.get("closed") or [] if isinstance(w, dict)
              and _number(w.get("resets_at")) is not None and _number(w.get("peak")) is not None]
    current = weeks.get("current")
    if not (isinstance(current, dict) and _number(current.get("resets_at")) is not None
            and _number(current.get("peak")) is not None):
        current = None
    return {"current": current, "closed": closed}


def update_weeks(weeks, seven_day):
    """Fold one reading of the seven-day window into the history. Returns (weeks, changed).
    A reading with a later reset than the one on file closes that week at its peak; a reading
    with an earlier reset (a stale session) is ignored."""
    pct = _number((seven_day or {}).get("used_percentage"))
    resets = _number((seven_day or {}).get("resets_at"))
    if pct is None or resets is None:
        return weeks, False
    current, closed = weeks["current"], list(weeks["closed"])
    if current is None:
        return {"current": {"resets_at": resets, "peak": pct}, "closed": closed}, True
    # resets_at can wobble by a second between payloads; the same week is within an hour
    if abs(resets - current["resets_at"]) < 3600:
        if pct <= current["peak"]:
            return weeks, False
        return {"current": {"resets_at": current["resets_at"], "peak": pct}, "closed": closed}, True
    if resets < current["resets_at"]:
        return weeks, False
    closed = [w for w in closed if abs(w["resets_at"] - current["resets_at"]) >= 3600] + [current]
    closed.sort(key=lambda w: w["resets_at"])
    return {"current": {"resets_at": resets, "peak": pct}, "closed": closed[-WEEKS_KEPT:]}, True


def recent_weeks(closed, current_reset):
    """The closed weeks among the last PLAN_WEEKS calendar weeks before the window that resets at
    `current_reset`. Weeks nobody used were never recorded, so this can hold fewer than PLAN_WEEKS;
    an old heavy week does not keep the verdict red, and light weeks spread over a year do not add
    up to four."""
    if current_reset is None:
        return []
    oldest = current_reset - PLAN_WEEKS * WINDOW_SECONDS["seven_day"] - 3600
    return [w["peak"] for w in closed if oldest <= w["resets_at"] < current_reset - 3600]


def plan_fit(record):
    """'tight', 'roomy', or None (fits, or too few recent weeks to say)."""
    limits = record.get("rate_limits") or {}
    spend = _number((limits.get("spend_limit") or {}).get("used_percentage"))
    recent = recent_weeks(record.get("plan_weeks") or [], _number((limits.get("seven_day") or {}).get("resets_at")))
    if (spend is not None and spend > 0) or any(p >= PLAN_TIGHT_AT for p in recent):
        return "tight"
    if len(recent) >= PLAN_WEEKS and all(p < PLAN_ROOMY_UNDER for p in recent):
        return "roomy"
    return None


def _seg_plan(r, now=None, color=True):
    """`Ultra`, `Ultra ↑` when it ran short (red), `Ultra ↓` when a smaller one would do (yellow)."""
    name = r.get("plan")
    if not name:
        return None
    fit = plan_fit(r)
    if fit == "tight":
        return paint(f"{name} \u2191", "red", color)
    if fit == "roomy":
        return paint(f"{name} \u2193", "yellow", color)
    return name


# The segments a line can show, in the order you list them. Each is a function of the record
# that returns a string or None (nothing to show, so the segment is skipped, not left blank).
def _seg_cost(r):
    usd = r.get("total_cost_usd")
    return None if usd is None else f"${usd:,.2f}"


# Colour: a window that is being spent faster than time passes is dull yellow; one close
# to empty is red (Ivan, 2026-10-14: "5h 67% · 7d 18% means we're definitely spending more
# than we should for day 1 of the week"). ANSI, which Claude Code renders in the status line.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
AHEAD_MARGIN = 2.0     # percentage points past the elapsed share before a window turns yellow
RED_AT = 90.0          # a window this full is nearly empty
CTX_YELLOW_AT, CTX_RED_AT = 70.0, 85.0
YELLOW, RED, RESET = "\033[33m", "\033[31m", "\033[0m"


def elapsed_share(resets_at, seconds, now):
    """How much of the window has passed, 0..1, from its reset time and length."""
    if resets_at is None or now is None:
        return None
    start = resets_at - seconds
    return max(0.0, min(1.0, (now - start) / seconds))


def window_state(pct, resets_at, seconds, now):
    """'red' near empty, 'yellow' when ahead of pace, else None."""
    if pct is None:
        return None
    if pct >= RED_AT:
        return "red"
    share = elapsed_share(resets_at, seconds, now)
    if share is not None and pct > share * 100.0 + AHEAD_MARGIN:
        return "yellow"
    return None


def paint(text, state, color):
    if not color or state is None:
        return text
    return (RED if state == "red" else YELLOW) + text + RESET


def _seg_window(name, label):
    def seg(r, now=None, color=True):
        win = (r.get("rate_limits") or {}).get(name, {})
        pct = win.get("used_percentage")
        if pct is None:
            return None
        state = window_state(pct, win.get("resets_at"), WINDOW_SECONDS[name], now)
        return paint(f"{label} {pct:.0f}%", state, color)
    return seg


def _seg_ctx(r, now=None, color=True):
    pct = r.get("context_used_percentage")
    if pct is None:
        return None
    state = "red" if pct >= CTX_RED_AT else "yellow" if pct >= CTX_YELLOW_AT else None
    return paint(f"ctx {pct:.0f}%", state, color)


ROOT_ENV = "GAS_GAUGE_ROOT"


def where(cwd, root=None):
    """Which project the session is in, from its working directory.

    Under `root` (a folder of projects, e.g. ~/code): the first folder below it, plus the
    worktree name when the path runs through `.worktrees/<name>`, so `~/code/webshop/src`
    is `webshop` and `~/code/webshop/.worktrees/fix-login` is `webshop/fix-login`; the root itself is its
    own name. Anywhere else: the nearest folder holding a `.git`, else the folder itself."""
    if not isinstance(cwd, str) or not cwd:
        return None
    path = Path(cwd)
    if root:
        base = Path(root).expanduser()
        parts = None
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            try:  # one side reached through a symlink
                parts = path.resolve().relative_to(base.resolve()).parts
            except (ValueError, OSError):
                pass
        if parts is not None:
            if not parts:
                return base.name or str(base)
            if ".worktrees" in parts:
                i = parts.index(".worktrees")
                if i + 1 < len(parts):
                    return f"{parts[0]}/{parts[i + 1]}" if i else parts[i + 1]
            return parts[0]
    try:
        for folder in (path, *path.parents):
            if (folder / ".git").exists():
                return folder.name or str(folder)
    except OSError:
        pass
    return path.name or str(path)


def _seg_model(r, now=None, color=True):
    """The model, and its own weekly window when it has one (with --usage-api): `fable-5-1 83%`.
    The window is coloured by pace like the others; the name is not."""
    model = r.get("model")
    if not model:
        return None
    name = str(model).replace("claude-", "")
    scoped = r.get("model_scoped")
    if not isinstance(scoped, dict):
        return name
    for label, win in scoped.items():
        if isinstance(win, dict) and label in str(model).lower():
            pct = _number(win.get("used_percentage"))
            if pct is None:
                break
            state = window_state(pct, _number(win.get("resets_at")), WINDOW_SECONDS["seven_day"], now)
            return f"{name} {paint(f'{pct:.0f}%', state, color)}"
    return name


def _compact(n):
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1_000:.0f}k" if n >= 1_000 else str(n)


def _seg_tokens(r):
    i, o = r.get("total_input_tokens"), r.get("total_output_tokens")
    if i is None and o is None:
        return None
    return f"{_compact(i or 0)} in / {_compact(o or 0)} out"


def _seg_session(r):
    return str(r["session_id"])[:8] if r.get("session_id") else None


def _seg_version(r):
    return f"cc {r['claude_code_version']}" if r.get("claude_code_version") else None


SEGMENTS = {
    "where": None,  # needs the root; drawn in status_line
    "cost": _seg_cost,
    "5h": _seg_window("five_hour", "5h"),
    "7d": _seg_window("seven_day", "7d"),
    "context": _seg_ctx,
    "ctx": _seg_ctx,  # the same segment by its label
    "model": _seg_model,
    "tokens": _seg_tokens,
    "session": _seg_session,
    "version": _seg_version,
    "plan": _seg_plan,
}
DEFAULT_SEGMENTS = ("where", "cost", "5h", "7d", "context", "model", "plan")
DEFAULT_SEPARATOR = " · "


COLOURED = {"5h", "7d", "context", "ctx", "model", "plan"}


def status_line(record, segments=DEFAULT_SEGMENTS, separator=DEFAULT_SEPARATOR, color=True, now=None, root=None):
    """The line, from the segments named, in that order. Unknown names are ignored, so a
    settings entry written for a newer version still prints something on an older one.
    `now` (epoch seconds) is when the line is drawn; the windows are coloured by pace against it.
    `root` is the folder of projects the `where` segment names a project in."""
    now = time.time() if now is None else now
    parts = []
    for name in segments:
        if name not in SEGMENTS:
            continue
        if name == "where":
            text = where(record.get("cwd"), root)
        elif name in COLOURED:
            text = SEGMENTS[name](record, now, color)
        else:
            text = SEGMENTS[name](record)
        if text:
            parts.append(text)
    return separator.join(parts)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="gas-gauge", add_help=True,
        description="A Claude Code status line: what the session costs, written to disk too.",
    )
    ap.add_argument("--segments", default=None,
                    help="comma-separated, in order; any of " + ", ".join(SEGMENTS) + f" (default: {','.join(DEFAULT_SEGMENTS)})")
    ap.add_argument("--separator", default=None, help=f"between segments (default: {DEFAULT_SEPARATOR!r})")
    ap.add_argument("--dir", default=None, help="where the per-session JSON files go (default: ~/.claude/usage-sidecar, or $GAS_GAUGE_DIR)")
    ap.add_argument("--root", default=None,
                    help=f"a folder of projects; `where` names the project under it (default: ${ROOT_ENV})")
    ap.add_argument("--no-file", action="store_true", help="print the line only; write nothing")
    ap.add_argument("--no-color", action="store_true", help="plain text (also: NO_COLOR in the environment)")
    ap.add_argument("--usage-api", action="store_true",
                    help="add the model's own weekly window to the model segment, read from the usage endpoint with your Claude Code login")
    ap.add_argument("--usage-api-fetch", action="store_true", help=argparse.SUPPRESS)  # the detached child
    return ap.parse_args(argv)


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as e:  # --help, or a bad flag: say so and still exit 0 (never break the prompt)
        return 0 if e.code in (0, None) else 0
    root_dir = Path(args.dir).expanduser() if args.dir else sidecar_root()
    if args.usage_api_fetch:
        try:
            fetch_usage(root_dir, token=login_token())
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                (root_dir / USAGE_LOCK).unlink()
            except OSError:
                pass
        return 0
    try:
        data = json.loads(sys.stdin.read() or "null")
        if not isinstance(data, dict):
            return 0
        record = record_from(data)
        if record is None:
            return 0
        now = time.time()
        if args.usage_api:
            try:
                scoped = fresh_scoped(read_usage(root_dir), now)
                if scoped is not None:
                    record["model_scoped"] = scoped
                refresh_usage_in_background(root_dir, now)
            except Exception:  # noqa: BLE001 - the rest of the line still draws
                pass
        segments = [p.strip() for p in args.segments.split(",") if p.strip()] if args.segments else DEFAULT_SEGMENTS
        weeks = None
        if "plan" in segments:
            # .weeks.json is read, changed and written without a lock: two sessions drawing in the
            # same few milliseconds can lose one reading. Peaks only ratchet up and the next reading
            # repairs the week, so a lock (and the waits it brings to a status line) is not worth it.
            try:
                record["plan"] = plan_name()
                weeks, changed = update_weeks(read_weeks(root_dir), (record.get("rate_limits") or {}).get("seven_day"))
            except Exception:  # noqa: BLE001 - the rest of the line still draws
                weeks, changed = None, False
            if changed and not args.no_file:
                try:
                    root_dir.mkdir(parents=True, exist_ok=True)
                    _write_json(root_dir / WEEKS_FILE, weeks)
                except Exception:  # noqa: BLE001 - an unwritable folder still shows the verdict
                    pass
        if not args.no_file:
            write_record(record, root_dir)
        if weeks is not None:
            record["plan_weeks"] = weeks["closed"]  # for the line only; the session file does not carry it
        color = not args.no_color and not os.environ.get("NO_COLOR")
        root = args.root or os.environ.get(ROOT_ENV) or None
        line = status_line(record, segments, args.separator if args.separator is not None else DEFAULT_SEPARATOR,
                           color=color, now=now, root=root)
        if line:
            print(line)
    except Exception:  # noqa: BLE001 - a status line must never fail
        return 0
    return 0
