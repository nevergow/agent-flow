# TASK SPEC — Claude Code 会话采集 + CLI worker 过滤修复

[Target]      /Users/wuzeqin/Projects/agent-flow （git 必须干净；只允许改 worker_runs.py；不要 commit / push）
[Objective]   1) 新增 `scan_claude()`，把 Claude Code 会话接进统一数据层；2) CLI `--worker all` 现在只含 dsh/pi，改成包含 codex / claude / hermes。
[Model]       deepseek-flash（所有执行者统一 flash）

## 背景：Claude Code 会话文件格式（已实测）

- 路径：`~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`，一行一条 JSON。
- 记录 `type`：`queue-operation` / `attachment` / `user` / `assistant` / `last-prompt`。
- 公共字段：`cwd`、`sessionId`、`timestamp`（ISO8601，带 Z）、`uuid`、`isSidechain`、`version`。
- `assistant` 记录：`message.model`、`message.usage = {input_tokens, output_tokens, cache_read_input_tokens, ...}`。
- `user` 记录：`message.content` 可能是字符串，也可能是 block 数组（`text` / `tool_result`）。
- `assistant` 的 content 数组里可能有 `tool_use` block：`{type:"tool_use", name, input}`。
- **关键坑：同一个 `message.id` 在文件里会重复出现 2–4 次**（流式/重放）。累计 token 必须按
  `message.id` 去重，每个 id 只计一次 usage（取该 id 最后一条即可）。
  实例：`~/.claude/projects/-private-tmp-wd-smoke/2956b300-*.jsonl` 去重后应为
  `input 31605 + output 23 = 31628`；不去重会变成 63256 —— 验收会卡这一点。

## 实现要求

- 新增常量：`CLAUDE_SESSIONS = os.path.join(HOME, ".claude", "projects")`。
- 新增函数 `scan_claude(days=7, limit=60)`：
  - `glob(<projects>/*/*.jsonl)`，先按 mtime 过滤（`>= time.time() - days*86400`）并按 mtime 倒序，
    **最多解析 `limit*3` 个文件**；结果按 `start` 倒序取前 `limit` 条。
  - rec 字段必须与现有扫描器（`scan_codex` / `scan_pi_sessions`）对齐：
    `worker="Claude"`、`kind="claude"`、`key=sessionId`（缺失则用文件名 stem）、`path=<jsonl 路径>`、
    `state`：`now - last_ts < 180` 判 `"RUNNING"`，否则 `"done"`；
    `tokens = tok_in + tok_out + tok_cache`；`cost_cny = cost_cny_of(tin, tout, tcache, start)`；
    `cost_usd=0.0`；`title/task/label` = 首条 user 文本（跳过以 `<` 开头的系统注入块，取第一行，`_one_line(..., 60)`）；
    `project = os.path.basename(cwd)`（cwd 取文件里第一条带 cwd 的记录）；`start/end` 为 epoch 秒；
    `segs = activity_segments([...所有记录 timestamp...])`（RUNNING 时传 `running=True`）；
    `turns` = user 记录数；`steps` = tool_use 数；`tools` = {name: count}；
    `tool_calls` = `[(ts, name, json.dumps(input)[:160]) ...]`；`last_text` = 最后一条 assistant 文本 `[:400]`；
    `model` = assistant `message.model`；`created/start`、`last/last_start` = 最后一条记录时间。
  - 任何解析异常都不能让整个扫描抛错（单文件失败就跳过）。
- `ALL_WORKERS` 增加 `"claude"`；`collect()` 在 `"claude" in workers` 时 `recs += scan_claude()`。
- CLI：`--worker` 的 `choices` 改为 `["all","dsh","pi","hermes","codex","claude"]`，
  `all` 映射为 `["dsh","pi","hermes","codex","claude"]`，帮助文本同步更新。

## Verification（必须贴真实命令输出）

1. `cd /Users/wuzeqin/Projects/agent-flow && python3 -c "import worker_runs as W; rs=W.scan_claude(); print('n=',len(rs)); [print(r['worker'], r['state'], r['tokens'], round(r['cost_cny'],4), r['label'][:36], r['project']) for r in rs[:3]]"`
   → 至少 1 条 Claude 记录。
2. 去重断言（必须等于 31628）：
   `python3 -c "import worker_runs as W; rs=[r for r in W.scan_claude() if '2956b300' in r['key']]; print([(r['tokens'], r['tok_in'], r['tok_out']) for r in rs])"`
   → 出现 `31628`；出现 63256 即判失败。
3. `python3 worker_runs.py --limit 10 --worker all | head -14` → 能看到 `Claude` 行（新采集的）。
4. `python3 worker_runs.py --worker codex --limit 2` → 不报错，输出 Codex 行（修复前会报 invalid choice）。

## 禁止

- 改其它文件（尤其 worker_dashboard.py）、改测试/断言、加依赖、commit、push。
- 不要为了让数字好看而硬编码 token 值。

[Execution Constraint] Maximum 3 consecutive retry attempts per identical error signature. If tests/compilation fail 3 times with the same root cause, immediately abort execution, revert uncommitted dirty states if applicable, and output a `[CIRCUIT_BREAKER_TRIGGERED]` flag along with the root cause bottleneck.
