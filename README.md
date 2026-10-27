# Role Play Vibing

**A team of AI roles for Claude Code. You talk to the CEO; a Researcher, an Engineer and a Judge do the work through tickets, woken by a dispatcher.**

[![ci](https://github.com/Gogi213/role-play-vibing/actions/workflows/ci.yml/badge.svg)](https://github.com/Gogi213/role-play-vibing/actions/workflows/ci.yml)
[![version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2FGogi213%2Frole-play-vibing%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&label=version)](CHANGELOG.md)
![platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-informational)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Commands](#commands) · [Configuration](#configuration) · [Development](#development) · [Changelog](CHANGELOG.md)

## Why

One long AI session piles up context, loses the thread and stalls while you are away. Role Play Vibing splits the work between roles and runs it through tickets: each task is one ticket and one short session of the right role, and a dispatcher wakes the next performer on its own.

- **Context stays small.** One task, one ticket, one short role session.
- **Work does not stall.** The dispatcher wakes the next role; the watcher catches stuck tickets.
- **Results are checked.** The Judge reviews every result against a "will be accepted if …" contract; only mechanical Haiku tasks are accepted by code instead.
- **No shortcuts for the sake of code.** A simplification is allowed only when a measurement shows the result is the same.

The core uses only the Python standard library: no packages or services to install.

## Quick start

Requires Python 3.11 (`python` on `PATH`), git, and the Claude Code CLI `claude` on `PATH`, signed in. Windows, macOS and Linux are supported.

1. Install the plugin in Claude Code:

   ```
   /plugin marketplace add https://github.com/Gogi213/role-play-vibing
   /plugin install role-play-vibing@role-play-vibing
   ```

2. In your project, run `/rpv-init`. It creates `.claude/roles` (role charters from templates), `.claude/tickets`, the owner's registry (`.claude/decisions.md`, `.claude/goals/`) and `.gitignore` entries. Existing files are not overwritten.
3. In the main session, run `/ceo`. The session becomes the CEO: it talks to you and files tickets.
4. Run `/rpv-start`. The dispatcher and the watcher start in the background and keep running after you close the Claude window. Running again restarts them, which picks up an updated plugin.
5. Give the CEO a task. `/rpv-doctor` shows whether everything is alive.

Platform details, manual start and project-root lookup: [docs/install.md](docs/install.md) (Russian).

### Updating

1. Update the plugin and restart Claude Code:

   ```
   claude plugin marketplace update role-play-vibing
   claude plugin update role-play-vibing@role-play-vibing
   ```

2. Run `/rpv-start` in the project: it restarts the dispatcher and the watcher on the new code.
3. Bring the role charters up to date. `/rpv-init` never overwrites `.claude/roles/`, so new rules from a release reach an existing project only through `python <plugin>/.claude/dispatcher/tickets.py charter sync [--file <name>.md]` (the CEO's guard rejects copying by hand; the release's entry in [CHANGELOG.md](CHANGELOG.md) names the changed files).

## How it works

![How Role Play Vibing works: owner, CEO, ticket file, dispatcher, roles, signals to the CEO, watcher, overseer, Telegram](docs/img/workflow.svg)

| Role | Does | Session | Model |
|---|---|---|---|
| CEO | talks to the owner, files tickets, reads signals | long-lived | your session |
| Secretary | the CEO hands it your words verbatim; it records them in the owner's registry as a decision (`В-N`), a goal (`G-NNN`), both, or nothing (a question, a status request, a one-off command) | one short run per call from the CEO | Haiku 5.5, effort xhigh |
| Researcher | protocol, calculation, report | one short run per wake | Sonnet 5.5, effort high (can be set per ticket) |
| Engineer | code, tests, machines | one short run per wake | Sonnet 5.5, effort xhigh |
| Judge | review and verdict | one short run per wake | Opus 5.5, effort high |
| Overseer | every 15 minutes, while tickets change: cost rules and a model reading the ticket logs; a finding goes to the Judge | a short process on a timer, no ticket | Haiku 5.5, effort xhigh |

Other helper subagents are not roles: the CEO uses `inbox` (triages signals) and `reporter` (status answers and daily-report lines); roles use `coder` and `reader`.

1. You tell the CEO what you want. The CEO hands your words, verbatim, to the Secretary, which records them in the registry. Then the CEO files a ticket, `.claude/tickets/TK-NNN.md` (a header with `status` and `next`, and a log of every step); `tickets.py new` refuses a ticket without `--ref` to a registry entry, so no work starts that the owner did not order.
2. Every 15 seconds the dispatcher (`dispatch.py`) reads the tickets and starts the role each one calls for (the owner of a new ticket, the role named in `next`, the Judge for finished work) as a short `claude -p` session.
3. The role reports its step with `tickets.py result <ID> <result> --why "…"`. Results: `done`, `pr`, `accept`, `return`, `blocked`, `ask-owner`, `wait`, `continue`. A routing table (`routes.py`), not the text of the entry, decides who is woken next: an executor's `done` or `pr` goes to the Judge, who answers `accept` (the ticket is done) or `return` (back to the executor).
4. When a ticket is done or stuck (`blocked`, `needs_owner`, `next: ceo`), the dispatcher writes a line to `ceo-inbox.md` and `ceo-wake.log`; a Monitor on `ceo-wake.log` wakes the CEO. The watcher (`watch.py`) adds orphaned tickets and dead waits. Details: [docs/signals.md](docs/signals.md) (Russian).
5. The CEO answers you. If Telegram is configured, you also get a daily report, the Judge's questions to you, and a "work stalled" alarm. The bot only sends.
6. The Overseer (`overseer.py`) sends its findings to the Judge, not to the CEO. Details: [docs/overseer.md](docs/overseer.md) (Russian).

Tickets can also wait for a file, another ticket, or a job on a remote machine from `~/.ssh/config` (`wait_for`): [docs/wait-for.md](docs/wait-for.md) (Russian).

**Team rules**

- Only the optimal algorithm; every report has an "Algorithm: …" line.
- Numbers go to the owner only after the Judge has checked them; an estimate is not a measurement.
- New tasks only on the owner's word.
- Work is handed over only with `--next`.
- Deleting is allowed only inside the project: a `PreToolUse` hook checks Bash, PowerShell and file edits, and also refuses to delete or overwrite paths the project marks as protected (`protected_paths` in `.claude/rpv.json`).
- Publishing (release, PR merge, push to main/master, tags, packages) only with the owner's consent on the ticket: `tickets.py allow <ID> publish --ref <decision id>`. Deleting remote branches needs the owner's word too, even for the CEO: `tickets.py allow-delete <branch> --ref <decision id>` (one hour, never main/master).
- No "accepted with a tail": if the executor reports something from the order as not done, the Judge returns it. Every acceptance states the tails (`--tail`): none (checked against the executor's submission), tickets already filed for them, or a task for the CEO.
- Live check: when the owner may look at the result (a page, a form, a report), the CEO writes the criterion as "`[вживую]` the owner opens X and sees Y within Z s". The executor checks it on the real machine and real data before handing in; the Judge opens it too and records what was seen, and the acceptance is refused without that record (`--live <file>`).
- A bug counts as fixed only with a check attached; the Judge runs the checks of all fixed bugs on every acceptance.
- The CEO cannot remove the Judge; only the owner can, with a direct quote.

Full charter: [`templates/roles/README.md`](templates/roles/README.md) (Russian; `/rpv-init` copies it to `.claude/roles/README.md` in your project).

## Commands

| Command | Does |
|---|---|
| `/rpv-init` | Prepares a project: `.claude/roles`, `notes/`, `tickets/`, the owner's registry, the error registry `errors/`, `.gitignore` entries. Existing files are kept. |
| `/ceo` | Marks the session as the CEO (hooks read the mark; it survives `/clear` in Desktop). |
| `/rpv-start` | Starts or restarts the dispatcher and the watcher in the background. |
| `/rpv-doctor` | One table with the health of the team: dispatcher, watcher, OS supervision, queue, errors of the last day. Read-only. |

Scripts run from the plugin folder with `--project <project>`:

| Script | Does |
|---|---|
| `dispatcher/tickets.py` | tickets: `new`, `comment`, `result`, `start`, `stop` (CEO only), `allow`, `allow-delete`, `wait`, `status`; `inbox` prints new lines of `ceo-inbox.md` |
| `dispatcher/registry.py` | the owner's registry of decisions and goals; written by the CEO only |
| `dispatcher/errors.py` | the error registry `.claude/errors/`: a bug is fixed only when its check failed before the fix and passes now |
| `dispatcher/report.py` | the daily report to the owner; `--dry-run` only prints ([docs/daily-report.md](docs/daily-report.md), Russian) |
| `dispatcher/notify.py` | a Telegram message to the owner: `send "text"`, `test` |
| `dispatcher/supervise.py` | OS supervision of the dispatcher and the watcher: `--install`, `--uninstall` |
| `dispatcher/release.py` | manual release: `bump` (version, commit, tag), `check` ([docs/release.md](docs/release.md), Russian) |

## Configuration

Three kinds of settings, all described in [docs/configuration.md](docs/configuration.md) (Russian):

- **Machine environment**: `RPV_PROJECT` (project root when `--project` is not given), `RPV_TG_TOKEN` and `RPV_TG_CHAT` (the owner's Telegram bot), `RPV_PLUGIN_DIR`.
- **Project file `.claude/rpv.json`**: protected paths, report time (`report_at`), parallelism limits, MCP servers per role, PR auto-merge, machines and job adapters. Edited by the CEO on the owner's word; closed to role runs.
- **Constants**: tick, timeouts, loop brakes, role models, watcher thresholds. Changed only by a plugin release.

## Development

```
python -m pip install pytest
python -m pytest .claude -q
```

Without pytest: `python -m unittest discover -s .claude/dispatcher`, then the same for `-s .claude/hooks`. Under pytest, `.claude/conftest.py` hides `RPV_*` settings and blocks real `ssh`; under `unittest` clear the environment yourself.

- **CI**: [`ci`](.github/workflows/ci.yml) runs the tests on Windows, macOS and Linux with Python 3.11–3.13 for every PR and every push to `main`.
- **Versions**: `python .claude/dispatcher/release.py check` verifies that `plugin.json`, `marketplace.json` and `CHANGELOG.md` agree.

```
commands/            slash commands
agents/              helpers: coder, reader, overseer; CEO helpers: secretary, inbox, reporter
hooks/               hooks.json and the hook launcher
tools/               fastcheck.py, jobrun.sh, benchmarks
templates/roles/     role charters copied into a project by /rpv-init
.claude/dispatcher/  dispatcher, watcher, tickets, doctor, supervision, release
.claude/hooks/       delete guard, CEO-signal guard, role context and memory
docs/                detailed guides (mostly in Russian)
```

More: [docs/reliability.md](docs/reliability.md) (OS supervision, downtime counter, move invariant), [`.claude/dispatcher/README.md`](.claude/dispatcher/README.md) (ticket statuses, dispatcher variables), [CHANGELOG.md](CHANGELOG.md).
