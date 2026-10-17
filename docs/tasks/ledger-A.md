# TASK SPEC — Slice A：切片账本数据层 `af_ledger.py`

```
[Target]      /Users/wuzeqin/Projects/agent-flow  (git 干净，HEAD 7b6bf15)
[Executor]    Pi 一次性
[Attempt]     1
[Anchor]      7b6bf156458b670e62ddb90027bb2499c02e1cc6
[Scope_Allow] af_ledger.py （仅此一个新文件）
[Scope_Forbid] 其余全部文件，尤其 worker_dashboard.py / worker_runs.py / agent_flow.py /
              README* / docs/ / 任何 .git 操作
```

## 1. Goal

新增 `af_ledger.py`：把「需求 → 切片 → 运行 → 验收」四张表落进 SQLite，并提供 CLI 与可导入的读接口，
供看板与后续 hook/shim 使用。只做数据层，不碰 UI、不碰网络、不碰 Hermes 配置。

## 2. Context

- 仓库是纯 stdlib 的 Python 单文件工具集（`worker_dashboard.py`、`worker_runs.py`、`agent_flow.py`），
  **不许新增第三方依赖**，Python 3.9+，macOS。
- 运行期数据目录：`~/.hermes/state/agent_flow/`（已存在，放 `tasks.json` 和 `dispatches/`）。
  新库默认放该目录下的 `ledger.db`；测试与演示必须用环境变量 `AF_LEDGER` 指到临时路径。
- 已有的 `af_dispatch.py` 会写 `dispatches/*.json`（旧格式，只读兼容，不要改它）。
- 本切片**不要**读 `~/.hermes/state.db` 以外的任何私有数据；`sync-cron` 只读 `~/.hermes/state.db`。

## 3. Scope & Boundaries

允许修改：**只有**新增 `af_ledger.py`。
禁止：改任何已有文件、改 `docs/`、执行 `git commit/push/reset/clean`、新增依赖、联网。

不变量：
- `import af_ledger` 不得产生副作用（不得建库、不得写盘）。
- 默认库路径 `~/.hermes/state/agent_flow/ledger.db`；`AF_LEDGER` 环境变量可覆盖。
- `selftest` 与 `seed-demo` **绝不**能写默认库：`seed-demo` 在 `AF_LEDGER` 未设置（或等于默认库）时必须
  以退出码 2 拒绝，并打印明确原因。
- 所有写操作在单个事务内完成；`PRAGMA journal_mode=WAL`、`busy_timeout=3000`。
- 时间统一用 epoch 浮点秒（本地时区无关）。

## 4. 数据模型（必须逐字一致，后续切片按这个读）

```sql
CREATE TABLE IF NOT EXISTS requests(
  id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slices(
  id TEXT PRIMARY KEY, req_id TEXT, goal TEXT NOT NULL, executor TEXT NOT NULL,
  scope TEXT, anchor TEXT, spec_path TEXT,
  attempt INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, slice_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  kind TEXT NOT NULL, label TEXT, pid INTEGER,
  start REAL NOT NULL, end REAL, exit_code INTEGER,
  evidence TEXT NOT NULL DEFAULT 'registry');
CREATE TABLE IF NOT EXISTS verdicts(
  id TEXT PRIMARY KEY, slice_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  result TEXT NOT NULL, reason TEXT, artifacts TEXT,
  ts REAL NOT NULL, verified_by TEXT NOT NULL DEFAULT 'hermes');
```

- `slices.state ∈ {planned, running, rejected, passed, aborted, failed}`，默认 `planned`。
- `verdicts.result ∈ {pass, reject, abort, na}`。
- 主键 id 规则：`req_<hex10>`、`slc_<hex10>`、`run_<hex10>`、`vrd_<hex10>`；
  也允许调用方用 `--id` 指定（重复则退出码 2，不静默覆盖）。
- `--retry` 语义：`verdict --result reject --retry` → 该切片 `attempt += 1`、`state='planned'`；
  不带 `--retry` 的 reject → `state='rejected'`。
- `verdict --result pass` → `state='passed'`；`abort` → `state='aborted'`。

## 5. CLI 契约（子命令名与参数必须一致）

```
af_ledger.py new-request --source <hermes|cron|cli|service> --text <TEXT> [--id ID] [--ts EPOCH]
af_ledger.py new-slice   --req REQ_ID --goal TEXT --executor <dsh|pi|rpc|subagent|cron|inline>
                         [--scope "a.py,b.py"] [--anchor HASH] [--spec PATH] [--id ID]
af_ledger.py start-run   --slice SLICE_ID --kind <dsh|pi|rpc|subagent|cron|inline>
                         [--attempt N] [--pid N] [--evidence <registry|process|inferred>] [--label TEXT]
af_ledger.py end-run     --run RUN_ID [--exit N] [--end EPOCH]
af_ledger.py verdict     --slice SLICE_ID --result <pass|reject|abort|na>
                         [--reason TEXT] [--artifacts "p1,p2"] [--retry] [--verified-by hermes]
af_ledger.py list        [--since 7d|24h|30d] [--limit N] [--json]
af_ledger.py stats       [--since 7d|24h|30d] [--json]
af_ledger.py show        SLICE_ID [--json]
af_ledger.py sync-cron   [--days N] [--json]
af_ledger.py seed-demo
af_ledger.py selftest
```

- 每个写命令成功时打印一行 JSON：`{"ok": true, "id": "<新 id>", ...}`（不要花哨格式）。
- 失败：stderr 一行原因，退出码 2（参数/约束错误）或 1（数据库错误）。
- `list --json` 结构：
  ```json
  {"count": 3, "since": "7d",
   "slices": [{"id":"slc_…","goal":"…","executor":"dsh","state":"passed","attempt":1,
               "req_id":"req_…","created":1792…,"updated":1792…,
               "runs":2,"last_verdict":"pass","cost_note":null}]}
  ```
- `stats --json` 结构（字段名逐字，看板直接用）：
  ```json
  {"since":"7d","slices_total":5,"verdict_slices":4,"passed_first_try":3,"passed_after_retry":1,
   "rejected":1,"open":0,"aborted":0,"pass_rate_first_try":0.75,
   "avg_attempts":1.4,"cron_runs":8,"cron_success_rate":1.0,
   "by_executor":{"dsh":{"total":3,"passed":2,"rejected":1},
                  "pi":{"total":2,"passed":2,"rejected":0}},
   "avg_minutes":12.5}
  ```
  口径（写进代码注释，且必须与 `list` 的可见明细一致）：
  - `verdict_slices` = 有实质裁决（`result in ('pass','reject','abort')`）的切片数，`na` 不算；
  - `pass_rate_first_try = passed_first_try / verdict_slices`（分母 0 时给 `0.0`）；
    **cron 切片是 `na` 裁决，绝不能进这个分母**，否则自动同步的 cron 会把通过率稀释成无意义数字；
  - `avg_attempts` 的分母同样只统计 `verdict_slices`；
  - `slices_total` 仍是窗口内全部切片（含 cron、含未结案）；
  - `cron_runs` = 窗口内 `kind='cron'` 的 run 数；`cron_success_rate` = `executor='cron'` 且
    `state='passed'` 的切片数 / `executor='cron'` 的切片数（无 cron 切片时 0.0）；
  - `avg_minutes` 取每个切片所有 run 的 `(end-start)` 之和的平均，没有 run 的切片不计。
- 文本模式（不带 `--json`）：`list`/`stats` 打印对齐的纯文本表格，适合终端看；不要 ANSI 颜色。

## 6. sync-cron 契约

- 只读打开 `~/.hermes/state.db`（`file:...?mode=ro`，uri=True）。
- 取 `select id, source, display_name, title, started_at, ended_at, end_reason from sessions
  where source='cron' and started_at >= ?`（窗口 `--days`，默认 7）。
- **job 名**：`display_name` 非空则用；否则取 `title` 里第一个 `" · "` 之前的部分；都没有则 `'cron'`。
- **job id**：从 `id` 用 `^cron_([0-9a-f]{6,})_` 捕获；捕获不到就用 job 名的 sha1。
- **一个 job 一个切片**：`id = "slc_cron_" + sha1(job_id)[:16]`，`goal = job 名`，`executor='cron'`。
- **一个 job 一条需求**：`id = "req_cron_" + sha1(job_id)[:10]`，`source='cron'`，`text=job 名`，
  `ts` = 该 job 最早会话的 `started_at`；切片的 `req_id` 指向它（`需求 → 切片` 视图不能有孤儿）。
- **状态映射**（取该 job 最近一次会话的 `end_reason`，全部小写后判断）：
  - 以 `cron_complete` 开头 → `passed`
  - 以 `cron_fail` / `cron_error` / `error` / `failed` 开头 → `failed`
  - 以 `cron_abort` / `interrupt` / `abort` / `cancel` 开头 → `aborted`
  - `cron_timeout` 开头 → `failed`
  - `ended_at is NULL` → `running`
  - 其它未知值 → `failed`，并把原 `end_reason` 写进该切片 `verdicts.reason`（不要丢信息）
- 每个 cron 会话写一条 `runs`：`id="run_cron_<session_id>"`、`kind='cron'`、`evidence='cron'`
  （**不是 registry**）、start/end 用会话时间、`exit_code` 留 NULL。
- 每个切片一条 `verdict`：`result='na'`、`reason` 记最近一次 `end_reason`。
- 幂等：重复同步不新建切片/需求/run；已存在的 run 若上次没 end、这次有，只补 `end`。
- Hermes DB 打不开时：打印警告，退出码 0（不要炸）。

## 7. 模块级读接口（给看板用）

```python
class LedgerError(Exception): ...
def read_slices(days=7, limit=50) -> dict      # 同 list --json 的结构
def read_stats(days=7) -> dict                 # 同 stats --json 的结构
def db_path() -> str                           # 生效的库路径（尊重 AF_LEDGER）
```
库不存在时 `read_slices`/`read_stats` 必须返回空结构（`{"count":0,"slices":[]}` /
全 0 的 stats），**不得**建库、不得抛异常。

## 8. Verification Contract（按顺序执行，全部粘贴真实输出）

1. `python3 -m py_compile af_ledger.py` → exit 0
2. `python3 af_ledger.py selftest` → exit 0，最后一行打印 `SELFTEST OK (10 checks)`；
   selftest 必须用 `tempfile` 建临时库，逐项断言：建库、new-request、new-slice、start-run、
   end-run、verdict pass、verdict reject+--retry 的 attempt 递增、stats 口径、list 的 runs 计数、
   重复 --id 被拒。任何断言失败打印 `FAIL: <项名>` 并退出码 1。
3. 真实 CLI 走一遍（把输出贴出来）：
   ```bash
   export AF_LEDGER=/tmp/afA_verify.db && rm -f "$AF_LEDGER"
   python3 af_ledger.py new-request --source hermes --text "验证需求"
   python3 af_ledger.py new-slice --req <上一步 id> --goal "验证切片" --executor pi --scope "af_ledger.py"
   python3 af_ledger.py start-run --slice <上一步 id> --kind pi --evidence registry
   python3 af_ledger.py end-run --run <上一步 id> --exit 0
   python3 af_ledger.py verdict --slice <切片 id> --result pass --reason "selftest"
   python3 af_ledger.py list --json
   python3 af_ledger.py stats --json
   ```
4. `python3 af_ledger.py seed-demo` 在 `AF_LEDGER` 未设置时 → 退出码 2 且不写库（贴输出）。
5. `AF_LEDGER=/tmp/afA_verify.db python3 af_ledger.py seed-demo` → exit 0，`list --json` 里 3 条切片。
6. `AF_LEDGER=/tmp/afA_cron.db python3 af_ledger.py sync-cron --days 2 --json` → exit 0，连跑两次；
   第二次 `new_runs` 必须为 0；随后用 python 断言（贴输出）：
   - `slices` 数量 ≥ 1，且没有 `goal='cron'` 的切片（除非真的取不到 title）
   - 每个 `executor='cron'` 切片的 `req_id` **非空**
   - `select distinct evidence from runs` 只有 `('cron',)`
   - `select distinct state from slices where executor='cron'` 只有 `('passed',)`（本机 cron 全是 cron_complete）
7. `git status --porcelain` → **只有** `?? af_ledger.py`（外加 /tmp 里的东西不算）。

## 9. Stop Conditions

- 需要改 `af_ledger.py` 以外的文件 → 停，走逃生通道。
- 需要第三方依赖 → 停。
- 同一错误签名出现 2 次 → 停。

## 10. Executor Rules（照抄，不许改）

1. 禁 `git commit / push / reset / clean`；禁改白名单外文件；禁改/删/跳过测试与验收脚本；
   禁 `# type: ignore` / 空 `except: pass` / 写死返回值凑验收。
2. 底层结构与本节不变量冲突时，停止修改并只输出：
   `[ABORT_CONTRACT_CONFLICT] <file:line> | <原因>`
3. 完成后按此格式回执（缺项视为失败）：
   ```text
   [STATUS]: DONE | ABORT_CONTRACT_CONFLICT
   [TOUCHED_FILES]: <与 git status 一致>
   [VERIFY]:
     1) <cmd> -> exit <code>
        <真实输出，5-20 行>
   [ARTIFACTS]: <无则 none>
   [NOTE]: <做了什么、跳过了什么、为什么>
   ```

---

## 11. Retry Context — Attempt 2

上次（Attempt 1）的代码主体通过，但 cron 建模有 5 处缺陷，**根因是我的任务书写错，不是实现失误**。
以下为可复现证据与精确位置：

```
[Evidence] 本机真数据：
  select id, display_name, title, end_reason from sessions where source='cron' limit 2
  → id='cron_73388cbfa312_20261018_000008', display_name=NULL,
    title='fx-usd-cny-monitor · Oct 18 00:01', end_reason='cron_complete'
[Observed] AF_LEDGER=/tmp/afcron.db python3 af_ledger.py sync-cron --days 2 --json
  → {"count": 1, "slices": 1, "sessions": 8, "new_runs": 8}
    select goal,state from slices → [('cron', 'aborted')]      ← 全机 cron 聚成一条 + 状态判错
    select distinct evidence from runs → [('registry',)]        ← 语义错
    select req_id from slices → [None]                          ← 需求树孤儿
[File:Line]
  af_ledger.py:656  name = (r["display_name"] or "").strip() or "cron"     ← 用 display_name 聚合（实为 NULL）
  af_ledger.py:619-628 _cron_state(...) 不认 'cron_complete'               ← 落到 else 分支 → aborted
  af_ledger.py:698  evidence 硬写 'registry'
  af_ledger.py:680  插入切片时 req_id 固定 NULL
  af_ledger.py:451-521 _stats_payload 的 pass_rate 分母含 cron(na) 切片
[New requirement] 见本文件 §5（stats 新增 verdict_slices / cron_runs / cron_success_rate，
  pass_rate 分母改为 verdict_slices）与 §6（job 名/ID 解析、状态映射、evidence='cron'、每个 job 一条需求）。
[Scope] 仍然只允许改 af_ledger.py；旧库不用做迁移（验收时我会删掉旧 ledger.db 重新 sync）。
[Anti-cheat] 不许为了通过验收把 cron_complete 特判成 passed 却把其它 reason 全归为 passed；
  §11 的映射表必须逐条实现，我会用伪造的 end_reason 造 4 条会话逐条验证。
