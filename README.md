# Agent Flow — a local dashboard for your coding agents

**English** · [简体中文](README.zh-CN.md)

> One picture that answers: **who is working, who dispatched what to whom,
> which stage each task is in, and what it cost.**

A local, zero-dependency dashboard that reads coding-agent session logs
(Hermes / DSH headless / Pi / Pi RPC / Codex / sub-agents) and renders them as
one live topology.

![screenshot](docs/screenshot.png)

## Why

Run a few coding agents in parallel and the terminal turns into fragmented logs.
You lose track of who is still running, where they are stuck, which task came
from which parent, and what all of it costs.

Agent Flow reads those session logs locally and draws a live "nodes + dispatch
edges" graph:

- **Nodes** — participants: you → orchestrator → workers (sandboxed long-chain /
  one-shot / persistent / sub-agent) → workspace
- **Edges** — one dispatch each, strictly directed; the same pair of nodes can
  have many edges (one per step), automatically fanned out so they stay distinct
- Only **running** edges animate (particles along the path); a new dispatch
  sends a pulse into the target. Nothing moves unless it means something.
- A Gantt strip of recent dispatches plus a 24-hour replay scrubber sit at the
  bottom.

## Quick start

Zero dependencies: Python 3.9+ standard library. The `zstd` CLI is used to read
DSH's compressed session logs (DSH is skipped if it is missing).

```bash
git clone <this-repo> agent-flow && cd agent-flow

python3 agent_flow.py --demo        # built-in demo data (fictional demo-app)
# open http://127.0.0.1:8799/

python3 agent_flow.py               # read this machine's real data
python3 agent_flow.py --port 9000 --window 30
```

| Flag | Purpose |
|---|---|
| `--port` | listen port, default 8799 (bound to 127.0.0.1 only) |
| `--window` | time window in minutes, default 15 |
| `--demo` | serve built-in demo data, read nothing from disk |
| `--backfill [days]` | rebuild history from old session logs (minute resolution, default 7 days) |
| `--history [hours]` | print a task table with cost to stdout |
| `--dump` | print one live snapshot as JSON (debugging the collector) |

Page option: `?static=1` disables animation (screenshots / low power).

## How to read the graph

- **Columns**: `INGRESS → ORCHESTRATOR → WORKERS → WORKSPACE`. Workers with no
  active or waiting task collapse into the **IDLE pool** on the right, so the
  main view stays readable.
- **Cards**: name / status pill (`RUN` running, `WAIT` finished and awaiting
  review, `IDLE` free) / current stage / session count / tokens in the last
  5 minutes / cost in window / a thin load bar.
- **Edges**: one dispatch each, fanned out when a pair has several; arrowheads
  are one-way.
- **Stage** is inferred from tool names in the last 3 minutes: `侦察` (read-only),
  `实现` (writes), `自检` (test commands), `收尾`.
- **Click** a node or an edge for details, including ready-to-paste commands
  (`worker_runs.py show <key>`, `tail -f <log>`).

UI strings are Chinese; the data model and API are language-neutral, so
translating the front end is a string-table change.

## Data sources (read-only — nothing is ever written to session files)

| Source | Default path | What is read |
|---|---|---|
| DSH headless | `~/.dsh/sessions/<cwd>/session-*/session.v*.jsonl.zstd` | task text, turn/step, per-call usage, tool calls |
| Pi one-shot | `~/.pi/agent/sessions/<cwd>/*.jsonl` | user task, per-turn usage |
| Pi RPC (persistent) | `~/.hermes/logs/pi-rpc-<name>.jsonl` | event stream, turn_end usage |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | `token_count` totals, tool calls, cwd |
| Hermes sub-agents | `~/.hermes/state.db` → `async_delegations` | real parent/child relation, state, tokens + cost in `result_json` |
| Hermes main loop | `~/.hermes/logs/agent.log` | last API / tool call timestamps |

History and replay samples live in `~/.hermes/state/agent_flow/tasks.json`
(one sample every 15 s, 7-day retention).

## Cost model

Every number is a **local estimate**, not a bill:

- DSH / Pi / Pi RPC / Codex: priced with deepseek-flash official rates
  (input ¥1, output ¥4, cache hit ¥0.02 per million tokens; doubled during
  Beijing-time weekday 09:00–12:00 and 14:00–18:00), summed per call
- Sub-agents: derived from `result_json.tokens` with the same table; falls back
  to Hermes' own `cost_usd × 7.1` when token detail is missing
- Token counts include cache hits — they measure **throughput**, not "new
  thinking"

## Dispatch registry: turn "who dispatched this" into a fact

By default the parent is inferred from the process tree and cwd (marked `推定`,
"inferred"). Wrap the launch in `scripts/af_dispatch.py` to write a registry
entry first and then `exec` the real command (same pid), so the dashboard can
mark it `登记` ("registered"):

```bash
scripts/af_dispatch.py --kind dsh --label "Step 12 cache warmup" \
    --task docs/tasks/step12.md -- \
    bash -lc 'cd ~/code/my-app && node ... headless - < docs/tasks/step12.md'
```

Registry entries are written to `~/.hermes/state/agent_flow/dispatches/*.json`.

## Known limitations

- **Sub-agents spawned inside DSH are invisible**: their session logs carry no
  back-reference to a parent
- **Parsers track current formats**: DSH session v4, Pi 1.x, Codex rollout v1 —
  upstream format changes require updates here
- **Single machine only**: no auth, no multi-host aggregation; the server binds
  to `127.0.0.1`
- System metrics (CPU/RAM) are deliberately not collected — this tool is about
  who is doing work, not how loaded the box is

## Layout

```
agent_flow.py             collector + built-in HTTP server (stdlib only)
agent_flow.html           single-page front end (vanilla JS + SVG, no build step)
scripts/af_dispatch.py    dispatch registry wrapper
docs/screenshot.png       demo-mode screenshot
```

## License

MIT
