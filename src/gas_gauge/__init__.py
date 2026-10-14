"""
gas-gauge — a Claude Code status line that shows what the session costs and writes it to disk.

Claude Code runs the statusLine command after every assistant message and pipes it a JSON
object (docs: https://code.claude.com/docs/en/statusline). This prints one line:

    $13.90 · 5h 17% · 7d 4% · context 42% · fable-5-1

and writes the same numbers to ~/.claude/usage-sidecar/<session_id>.json ($GAS_GAUGE_DIR or
$COST_SIDECAR_DIR overrides the folder), so your own tooling can read a session's cost without
anyone pasting /usage.

Rules of the road for a status line: never fail, never block, never write outside its folder.
Bad or missing input prints nothing and exits 0.

Known limits, both shared with /usage itself: the total resets on /clear (a new session id, so
a new file) and restarts from zero when a session is resumed (same id; the file keeps the
earlier peak as peak_total_cost_usd). The per-model weekly window is not in the payload.
"""


import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
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
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


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
    return paint(f"context {pct:.0f}%", state, color)


def _seg_model(r):
    return str(r["model"]).replace("claude-", "") if r.get("model") else None


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
    "cost": _seg_cost,
    "5h": _seg_window("five_hour", "5h"),
    "7d": _seg_window("seven_day", "7d"),
    "context": _seg_ctx,
    "ctx": _seg_ctx,  # alias; the gauge has nothing to do with the ctx tool, so the label says "context"
    "model": _seg_model,
    "tokens": _seg_tokens,
    "session": _seg_session,
    "version": _seg_version,
}
DEFAULT_SEGMENTS = ("cost", "5h", "7d", "context", "model")
DEFAULT_SEPARATOR = " · "


COLOURED = {"5h", "7d", "context", "ctx"}


def status_line(record, segments=DEFAULT_SEGMENTS, separator=DEFAULT_SEPARATOR, color=True, now=None):
    """The line, from the segments named, in that order. Unknown names are ignored, so a
    settings entry written for a newer version still prints something on an older one.
    `now` (epoch seconds) is when the line is drawn; the windows are coloured by pace against it."""
    now = time.time() if now is None else now
    parts = []
    for name in segments:
        seg = SEGMENTS.get(name)
        if seg is None:
            continue
        text = seg(record, now, color) if name in COLOURED else seg(record)
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
    ap.add_argument("--no-file", action="store_true", help="print the line only; write nothing")
    ap.add_argument("--no-color", action="store_true", help="plain text (also: NO_COLOR in the environment)")
    return ap.parse_args(argv)


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as e:  # --help, or a bad flag: say so and still exit 0 (never break the prompt)
        return 0 if e.code in (0, None) else 0
    try:
        data = json.loads(sys.stdin.read() or "null")
        if not isinstance(data, dict):
            return 0
        record = record_from(data)
        if record is None:
            return 0
        if not args.no_file:
            write_record(record, Path(args.dir).expanduser() if args.dir else sidecar_root())
        segments = [p.strip() for p in args.segments.split(",") if p.strip()] if args.segments else DEFAULT_SEGMENTS
        color = not args.no_color and not os.environ.get("NO_COLOR")
        line = status_line(record, segments, args.separator if args.separator is not None else DEFAULT_SEPARATOR, color=color)
        if line:
            print(line)
    except Exception:  # noqa: BLE001 - a status line must never fail
        return 0
    return 0
