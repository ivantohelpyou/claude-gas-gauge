# claude-gas-gauge

**A fuel gauge for Claude Code.** One line under the prompt, after every message:

```
webshop · $13.90 · 5h 17% · 7d 4% · ctx 42% · fable-5-1
```

Which project the session is in, what the session would cost at API rates so far, how much of the plan's five-hour and seven-day
windows is used, how full the context window is, and which model is answering. The same numbers
go to a small JSON file per session, so your own tooling can read what a session cost without
anyone pasting `/usage` into anything.

No dependencies, no daemon, no network. A hundred and fifty lines of Python that read the JSON
Claude Code pipes to its status line and never fail: bad or missing input prints nothing and
exits 0.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/ivantohelpyou/claude-gas-gauge   # installs `gas-gauge`
```

Then in `~/.claude/settings.json`:

```json
{
  "statusLine": { "type": "command", "command": "gas-gauge" }
}
```

The line appears after the next assistant message. Claude Code's own docs for the status line are
at https://code.claude.com/docs/en/statusline.

## Reading it

| Segment | Meaning | What to do with it |
|---|---|---|
| `webshop` | the project the session is in (see `--root` below) | is this the window you meant to type into? |
| `$13.90` | the session's cost so far at API list price (`cost.total_cost_usd`): a price, not a bill, if you are on a flat plan | compare sessions; log it; notice when one runs away |
| `5h 17%` | the plan's five-hour rolling window, used | past 80% with hours to the reset: finish the turn, start nothing wide |
| `7d 4%` | the plan's seven-day window, used; the binding constraint on a flat plan | pace it: a day is 14% of a week |
| `ctx 42%` | how full the context window is; every later turn re-reads all of it | past 70%, a checkpoint and `/clear` is cheaper than ten more turns |
| `fable-5-1` | the model answering | is it the one you meant? |

The two windows appear only for claude.ai subscription accounts, and only after the first API
response. The per-model weekly window that `/usage` draws is not in the payload Claude Code
provides, so it is not on the line.

## Configure

The line is yours. Pick the segments and their order, and the separator, in the settings entry:

```json
{ "statusLine": { "type": "command", "command": "gas-gauge --segments cost,7d,context --separator ' | '" } }
```

| Segment | Shows |
|---|---|
| `where` | `webshop`, `webshop/fix-login` |
| `cost` | `$13.90` |
| `5h` | `5h 17%` |
| `7d` | `7d 4%` |
| `context` | `ctx 42%` (`ctx` is accepted as a name too) |
| `model` | `fable-5-1` |
| `tokens` | `305k in / 4k out` |
| `session` | the first eight characters of the session id |
| `version` | `cc 2.1.299` |

Default: `where,cost,5h,7d,context,model`. A segment with nothing to show is skipped, not left blank; a
name this version does not know is ignored.

`where` names the git repository the session's folder is in, or the folder itself outside one.
If your projects live under one folder, pass it as `--root` (or set `GAS_GAUGE_ROOT`): the
segment then names the first folder below it, plus the worktree when the path runs through
a folder named `.worktrees` (the layout where a project keeps its git worktrees at
`<project>/.worktrees/<name>`; other layouts just show the project). With `--root ~/code`, `~/code/webshop/src` is `webshop` and
`~/code/webshop/.worktrees/fix-login` is `webshop/fix-login`. `--dir PATH` moves the JSON files; `--no-file` prints
the line and writes nothing. A bad flag prints nothing and exits 0: a status line must never break
the prompt.

## Colour

The two windows are coloured by pace. A window being spent faster than time passes is dull
yellow: on day one of the week, `7d 18%` is yellow, because a seventh of the week has gone and
more than a seventh of it is spent. A window at 90% or more is red. The `ctx` segment is
yellow from 70% and red from 85%. `--no-color` or `NO_COLOR` in the environment turns it off.

The gauge is independent of everything else the author publishes. It does not need, know about
or use the `ctx` context tool; the `context` segment is Claude Code's own context window.

## The file

`~/.claude/usage-sidecar/<session_id>.json`, rewritten atomically after every message
(`$GAS_GAUGE_DIR` overrides the folder):

```json
{
  "session_id": "abe34506-…",
  "transcript_path": "/home/you/.claude/projects/-home-you-repo/abe34506-….jsonl",
  "cwd": "/home/you/repo",
  "model": "claude-fable-5-1",
  "total_cost_usd": 13.90,
  "peak_total_cost_usd": 13.90,
  "total_input_tokens": 305017,
  "total_output_tokens": 4102,
  "context_used_percentage": 42,
  "rate_limits": {
    "five_hour": { "used_percentage": 17, "resets_at": 1792008600 },
    "seven_day": { "used_percentage": 4, "resets_at": 1792591200 }
  },
  "claude_code_version": "2.1.299",
  "written_at": 1792000000.0
}
```

Two things a reader should know. `total_cost_usd` resets on `/clear` (a new session id, a new
file) and restarts from zero when a session is resumed (same id); the file keeps the highest
figure any run wrote as `peak_total_cost_usd`, so price a session at the higher of the two. And
the first message after a resume carries no cost yet; a null never overwrites a number already
on file.

## A second row

Claude Code draws every line the command prints, and re-runs it every `refreshInterval`
seconds as well as after each message. So another script's line can sit under the gauge and
keep moving while the session is idle, for instance a row for whatever long job is running:

```json
{ "statusLine": { "type": "command", "refreshInterval": 15,
                  "command": "in=$(cat); printf %s \"$in\" | gas-gauge; printf %s \"$in\" | your-jobs-script" } }
```

Both get the same JSON, so the second script can read `session_id` and tell this session's work
from everyone else's. It should print nothing when it has nothing to say.

What you build on the file is up to you. The author's own ledger reads it to give every session a
row, charged to the work it did.

## Tests

```bash
uv run --with pytest pytest -q
```

## License

MIT.
