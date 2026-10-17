# TASK SPEC — Slice B：主看板 `SLICES` 面板

```
[Target]       /Users/wuzeqin/Projects/agent-flow
[Executor]     DSH headless
[Attempt]      1
[Anchor]       645d298cfeb6198a73489edcfe00096e2f775183（开工前 git status --porcelain 必须为空）
[Scope_Allow]  worker_dashboard.py, README.md, README.zh-CN.md
[Scope_Forbid] 其余全部文件；特别是 af_ledger.py（数据层已验收，接口只读）、worker_runs.py、
               agent_flow.py/html、docs/、任何 .git 写操作
```

## 1. Goal

在像素风主看板里新增一个 `SLICES`（切片）面板：按需求分组显示「需求 → 切片 → 执行者 → 尝试次数 →
状态 → 最近裁决 → 运行次数 → 更新时间」，标题栏右侧给出窗口内汇总与范围控件。数据只读自
`af_ledger`，缺库必须 fail-open。

## 2. Context

**数据层已存在并通过验收**（`af_ledger.py`，不要改它）：

```python
import af_ledger as L
L.db_path()                      # 生效的库路径，尊重 AF_LEDGER
L.read_slices(days=7, limit=50)  # {"count","since","slices":[...]}
L.read_stats(days=7)             # {"slices_total","verdict_slices","pass_rate_first_try",
                                 #  "avg_attempts","cron_runs","cron_success_rate", ...}
# 切片字段：id, goal, executor, state, attempt, req_id, req_text, created, updated,
#           runs, last_verdict, cost_note
# state ∈ planned | running | rejected | passed | aborted | failed
# last_verdict ∈ pass | reject | abort | na | None
# 缺库时 read_* 返回空结构，不建库、不抛异常
```

**看板渲染约定**（`worker_dashboard.py`）：

- 纯 HTML/SVG，无外部 JS/CDN；像素风样式都在 `CSS_PIXEL`，控件样式在 `CTL_CSS`。
- 文案一律走 `I18N` + `T("key")`，zh/en 两份必须齐全，**不许硬编码中文或英文**。
- 卡片控件放各自标题栏右侧，用 `_ctl_grp(key, [(value,label)...], current)`；链接参数由 `_qs()` 合并
  `CUR`，不要手拼 query。
- 4 张 KPI 卡是 `grid-template-columns:repeat(4,1fr)`，**不要加第 5 张卡**（会破版）。
- 现有面板顺序：TOKEN 图 / 时间线 / 项目 / 进程（`grid2`）→ 派活拓扑（`grid1`）→ NOW RUNNING →
  RUN HISTORY。新面板插在**派活拓扑之后、NOW RUNNING 之前**。
- `render_pixel(...)` 参数已含 `cur`；新范围参数要像 `tdays` 一样全链路透传：
  `serve()` 的 `pick()`、`render_pixel()`、`charts_html()`/新增函数、`main()` 的 CLI 参数。

## 3. 面板规格（照做，不要自行发挥）

- 标题：`T("p_slices")`，zh=`切片`，en=`SLICES`。
- 标题栏右侧：范围控件 `sdays` ∈ {7,14,30}（文案 `7D/14D/30D`）+ 一个汇总 tag：
  `T("slices_tag", total, verdict, pass_pct, avg)`，zh=`%d 切片 · %d 已裁决 · 一次通过 %d%% · 平均 %.1f 次`
  （`pass_pct = round(pass_rate_first_try*100)`），数字全部来自 `read_stats()`，**不许自己重算**。
- 表体：`<table class="slices">`，列固定为
  `状态 | 切片 | 执行者 | 尝试 | 裁决 | RUNS | 更新`（宽度 84/auto/96/56/64/56/96）。
- **按需求分组**：把 `read_slices()` 返回的 slices 按 `req_id` 归组（保持原顺序，组内按 `created` 升序），
  每组先输出一行 `<tr class="req">`：`<td colspan="7">` + `req_text`（截 60 字，空则回退 `req_id`）。
- 状态 pill：复用 `.pill`；映射 `passed→p-ok`、`rejected→p-warn`、`running→p-run`、
  `failed|aborted→p-bad`、`planned→p-ok` 但文案走 `T()` 新键（`sl_planned/sl_running/sl_passed/
  sl_rejected/sl_failed/sl_aborted`，zh 计划中/在跑/通过/打回/失败/中止）。
- 执行者：复用 `worker_badge()`；`executor in ("cron","inline")` 用新 class `b-cron`
  （在 `CSS_PIXEL` 追加一条：`background:var(--amber-soft);color:var(--amber)`）。
- 空状态：一行 `colspan="7"`，`T("slices_empty")`，zh=`还没有切片记录 · 用 af_ledger.py 记一条`。
- 表格底部一行：`T("slices_show", shown, total)`，zh=`显示 %d / 共 %d 条（窗口内）`，
  当 `shown < total` 时可见（`limit=50`），相等时也显示，便于核对。
- 所有 `<td>` 里的时间一律 `%m-%d %H:%M` 本地时区；数字用 `font-variant-numeric:tabular-nums`。

**fail-open**：整个读取 + 渲染包在 `try/except Exception`，任何异常 → 打印空状态面板，页面照常渲染。

## 4. README 更新

- `README.md` / `README.zh-CN.md` 各加一小段（3-5 行）说明 `SLICES` 面板与它读 `af_ledger.py`
  的账本（`~/.hermes/state/agent_flow/ledger.db`），并给出记一条切片的最小命令：
  `python3 af_ledger.py new-slice --req <ID> --goal "…" --executor pi`。中英两份内容对应。

## 5. Verification Contract（按顺序执行，全部粘贴真实输出）

1. `python3 -m py_compile worker_dashboard.py af_ledger.py` → exit 0
2. 空库 fail-open：
   ```bash
   AF_LEDGER=/tmp/sliceB_empty.db python3 worker_dashboard.py --style pixel --out /tmp/B_empty.html --no-refresh
   grep -c "还没有切片记录" /tmp/B_empty.html        # ≥1
   ```
3. 种子库 + 数字勾稽：
   ```bash
   export AF_LEDGER=/tmp/sliceB.db && rm -f $AF_LEDGER
   R=$(python3 af_ledger.py new-request --source hermes --text "看板验收需求" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
   S1=$(python3 af_ledger.py new-slice --req $R --goal "一次通过" --executor pi | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
   S2=$(python3 af_ledger.py new-slice --req $R --goal "打回后通过" --executor dsh | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
   S3=$(python3 af_ledger.py new-slice --req $R --goal "未结案" --executor cron | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
   python3 af_ledger.py start-run --slice $S1 --kind pi >/dev/null
   python3 af_ledger.py verdict --slice $S1 --result pass >/dev/null
   python3 af_ledger.py start-run --slice $S2 --kind dsh >/dev/null
   python3 af_ledger.py verdict --slice $S2 --result reject --retry >/dev/null
   python3 af_ledger.py start-run --slice $S3 --kind cron >/dev/null
   python3 worker_dashboard.py --style pixel --out /tmp/B_full.html --no-refresh
   python3 af_ledger.py stats --json
   ```
   断言（贴出命令输出）：
   - `/tmp/B_full.html` 里 `class="req"` 出现 1 次（1 条需求）
   - 切片行 3 行（`grep -c '<tr class="sl"'` = 3）
   - `slices_tag` 里的 `切片数/已裁决/通过率/平均次数` 与 `stats --json` 的
     `slices_total / verdict_slices / pass_rate_first_try / avg_attempts` 完全一致
4. 英文版无中文残留：
   ```bash
   AF_LEDGER=/tmp/sliceB.db python3 worker_dashboard.py --style pixel --lang en --out /tmp/B_en.html --no-refresh
   grep -P '[\x{4e00}-\x{9fff}]' /tmp/B_en.html | head    # 必须为空
   ```
5. 现有面板未丢（zh 与 en 各查一次）：`时间线`/`派活拓扑`/`运行流水`、`TIMELINE`/`DELEGATION GRAPH`/`RUN HISTORY`
   都还在；`python3 worker_dashboard.py --style clean --out /tmp/B_clean.html --no-refresh` → exit 0
6. 范围参数全链路：`python3 worker_dashboard.py --style pixel --out /tmp/B30.html --no-refresh --sdays 30` →
   exit 0，且 `/tmp/B30.html` 里 `.pctl a` 的 href **同时**包含 `sdays` 与 `tdays/days/graph/rows/projs/scope`
   （用一行 python 断言所有 `.pctl a` 的 href 都含这些键，贴输出）。
7. `git status --porcelain` → 只有 `README.md`、`README.zh-CN.md`、`worker_dashboard.py` 三个 `M`。

## 6. Stop Conditions

- 需要改 `af_ledger.py` 或白名单外文件 → 停，走逃生通道。
- 需要新增第三方依赖 / 外部 CDN → 停。
- 同一错误签名出现 2 次 → 停。

## 7. Executor Rules（照抄，不许改）

1. 禁 `git commit / push / reset / clean`；禁改白名单外文件；禁改/删/跳过测试与验收脚本；
   禁 `# type: ignore` / 空 `except: pass` / 写死返回值凑验收。
2. 与本节不变量冲突时停止并只输出：`[ABORT_CONTRACT_CONFLICT] <file:line> | <原因>`
3. 回执格式（缺项视为失败）：

```text
[STATUS]: DONE | ABORT_CONTRACT_CONFLICT
[TOUCHED_FILES]: <与 git status 一致>
[VERIFY]:
  1) <cmd> -> exit <code>
     <真实输出 5-20 行>
[ARTIFACTS]: /tmp/B_full.html, /tmp/B_en.html
[NOTE]: <做了什么、跳过什么、为什么>
```
