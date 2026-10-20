#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker_dashboard.py — 把 worker_runs 的数据渲染成一页浅色看板（HTML）。

数据全部来自 worker_runs.py（它才是唯一的数据源解析器），这里只负责排版。

用法
====
  worker_dashboard.py                # 生成 ~/.hermes/cache/worker-dashboard.html
  worker_dashboard.py --open         # 生成后直接用默认浏览器打开
  worker_dashboard.py --no-refresh   # 不做 5 秒自动刷新（截图/预览用）
  worker_dashboard.py --out /tmp/x.html --refresh 3
"""

import argparse
from urllib.parse import urlencode
import glob
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker_runs as W  # noqa: E402
try:
    import af_ledger as L  # noqa: E402
except Exception:
    L = None                 # 数据层不可用时由 slices_panel 降级为空状态

TZ = timezone(timedelta(hours=8))

# 数据源指纹（只 stat，不解析）：轮询时先比它，没变化就直接复用上次渲染结果。
# 有变化才重新采集/渲染 —— 这是「只在变化时刷新」的服务端那一半。
_SIG_PATTERNS = [
    os.path.join(W.DSH_SESSIONS, "*", "session-*", "*.jsonl.zstd"),
    os.path.join(W.PI_SESSIONS, "*", "*.jsonl"),
    os.path.join(W.PI_RPC_LOGS, "pi-rpc-*.jsonl"),
    os.path.join(W.CODEX_DIR, "**", "*.jsonl"),
    os.path.join(W.HOME, ".hermes", "state", "pi-rpc", "*", "meta.json"),
    W.HERMES_DB, W.HERMES_DB + "-wal",
    os.path.join(W.HOME, ".hermes", "state", "agent_flow", "ledger.db"),
    os.path.join(W.HOME, ".hermes", "state", "agent_flow", "ledger.db-wal"),
]


def src_sig():
    items = []
    for pat in _SIG_PATTERNS:
        try:
            paths = glob.glob(pat, recursive=True)
        except Exception:
            continue
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append((p, int(st.st_mtime), st.st_size))
    items.sort()
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()


# 片段标记：服务端把页面切成几个可独立替换的块，浏览器只换变化的那块，不整页重载。
FRAG_RE = re.compile(r"<!--F:([a-z]+)-->(.*?)<!--F:/\1-->", re.S)

# 每秒都在变的时钟/耗时不算「变化」：算哈希前先把它们抹平，
# 否则每个块每 5 秒都会“变”，增量刷新就退化成整页重绘。
# 展示值仍然是真实时间（客户端每秒自己走）。
_STABLE_MASKS = [
    (re.compile(r'(<div class="t px" id="clk">)[^<]*(</div>)'), r"\1<CLK>\2"),
    (re.compile(r'(<span data-since="\d+">)[^<]*(</span>)'), r"\1<EL>\2"),
]

# 各块最短刷新间隔（秒）：图表/拓扑里在跑任务的长条会随时间变，没必要每 5 秒重画
FRAG_HINTS = {"top": 5, "live": 5, "table": 10, "slices": 30, "network": 30, "charts": 30}


def stable_text(body):
    for rx, rep in _STABLE_MASKS:
        body = rx.sub(rep, body)
    return body


def split_frags(doc):
    frags, hashes = {}, {}
    for name, body in FRAG_RE.findall(doc):
        frags[name] = body
        hashes[name] = hashlib.sha1(stable_text(body).encode("utf-8")).hexdigest()[:12]
    return frags, hashes
CACHE = os.path.join(os.path.expanduser("~"), ".hermes", "cache", "worker-dashboard.html")

STATE_CLASS = {
    "RUNNING": "run",
    "done": "ok",
    "interrupted": "warn",
    "aborted": "warn",
    "FAILED": "bad",
}
STATE_LABEL = {
    "RUNNING": "运行中",
    "done": "完成",
    "interrupted": "中断",
    "aborted": "已中止",
    "FAILED": "失败",
}


def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def human_tok(n):
    if not n:
        return "—"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}K"
    return f"{n / 1e6:.1f}M"


FX = 7.1          # 美元→人民币，只用于兼容旧记录里只有 $ 的情况


def rec_cny(r):
    """统一人民币口径：六种执行者都算 cost_cny（worker_runs 统一按官价估）。"""
    if r.get("cost_cny") is not None:
        return r.get("cost_cny") or 0.0
    return (r.get("cost_usd") or 0.0) * FX


def cost_text(r):
    v = rec_cny(r)
    if not v:
        return "—"
    return f"≈¥{v:.3f}" if v < 0.01 else f"≈¥{v:.2f}"


def cost_tip(r):
    """悬停说明这行钱是怎么来的：谁、多少 token、按什么价。"""
    v = rec_cny(r)
    usd = r.get("cost_usd") or 0
    parts = [f"{r.get('worker')} · {cost_text(r)} {T('cost_est')}",
             T("cost_tokens", f"{r.get('tok_cache') or 0:,}",
               f"{r.get('tok_in') or 0:,}", f"{r.get('tok_out') or 0:,}")]
    if r.get("worker") != "DSH" and usd:
        parts.append(T("cost_upstream", usd))
    parts.append(T("price_note"))
    return " · ".join(parts)


def dur_sec(r):
    """活跃时长：有活跃区间就用区间之和（空闲 >20min 断开），否则退回 end-start。"""
    segs = r.get("segs")
    if segs:
        return W.seg_seconds(segs)
    end = r.get("end") or time.time()
    return max(0, end - (r.get("start") or end))


def dur_text(r):
    sec = int(dur_sec(r))
    if r["state"] == "RUNNING":
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def clean_title(s):
    s = (s or "").strip()
    while s[:1] in "#*- " and s:
        s = s.lstrip("#*- ").strip()
    s = s.replace("`", "").replace("**", "")
    return s or T("untitled")


CSS = """
:root{
  --paper:#F7F6F3; --card:#FFFFFF; --ink:#171A1F; --ink-2:#5B6167; --ink-3:#9AA0A6;
  --line:#E8E5DF; --line-2:#F0EDE8; --accent:#4338CA; --accent-soft:#EEF0FE;
  --run:#1B7F4B; --run-soft:#E8F6EE; --warn:#9A6700; --warn-soft:#FFF6E5;
  --bad:#B42318; --bad-soft:#FDEDEC; --pi:#0F766E; --pi-soft:#E6F4F1;
  --rpc:#8A5A00; --rpc-soft:#FBF3E3; --shadow:0 1px 2px rgba(20,22,28,.05), 0 8px 24px -16px rgba(20,22,28,.18);
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:34px 30px 44px; background:var(--paper); color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","PingFang SC",sans-serif;
  font-feature-settings:"tnum" 1; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px;margin:0 auto}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:6px 0 22px}
h1{font-size:28px;line-height:1.15;letter-spacing:-.022em;margin:0;font-weight:640}
.sub{color:var(--ink-2);font-size:13.5px;margin-top:6px}
.stamp{text-align:right;font-size:12px;color:var(--ink-3);white-space:nowrap}
.stamp b{color:var(--ink-2);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:15px 17px 16px;box-shadow:var(--shadow)}
.card .k{font-size:11.5px;color:var(--ink-3);font-weight:600;letter-spacing:.04em}
.card .v{font-size:25px;letter-spacing:-.02em;font-weight:640;margin-top:7px;display:flex;align-items:baseline;gap:7px}
.card .v small{font-size:12.5px;color:var(--ink-3);font-weight:500;letter-spacing:0}

.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.panel + .panel{margin-top:18px}
.panel-h{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:15px 20px;border-bottom:1px solid var(--line-2)}
.panel-h h2{font-size:14px;margin:0;font-weight:640;letter-spacing:-.01em}
.panel-h .hint{font-size:12px;color:var(--ink-3)}

.live{padding:20px 20px 18px}
.live-top{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;align-items:flex-start}
.live-title{font-size:17.5px;font-weight:640;letter-spacing:-.015em;margin:0 0 7px;line-height:1.32;max-width:760px}
.live-meta{font-size:12.5px;color:var(--ink-2);display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.live-meta .sep{color:#D5D1C9}
.chip{display:inline-block;font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-2);
  background:#F6F4F0;border:1px solid var(--line);border-radius:999px;padding:6px 10px;font-variant-numeric:tabular-nums}
.dot{width:7px;height:7px;border-radius:50%;background:var(--run);box-shadow:0 0 0 0 rgba(27,127,75,.45);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(27,127,75,.38)}70%{box-shadow:0 0 0 9px rgba(27,127,75,0)}100%{box-shadow:0 0 0 0 rgba(27,127,75,0)}}

.timeline{margin:18px 0 0;padding:0;list-style:none;border-top:1px solid var(--line-2)}
.timeline li{display:grid;grid-template-columns:58px 76px minmax(0,1fr);gap:11px;align-items:center;
  padding:8px 0;font-size:12.8px;color:var(--ink-2);border-bottom:1px solid var(--line-2)}
.timeline li:last-child{border-bottom:0;padding-bottom:0}
.timeline time{font-variant-numeric:tabular-nums;color:var(--ink-3);font-size:12px}
.tool{display:inline-block;text-align:center;font:600 11.5px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);
  background:var(--accent-soft);padding:4px 6px;border-radius:6px}
.arg{color:var(--ink-2);font:11.9px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

table{width:100%;border-collapse:collapse;table-layout:fixed}
thead th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);font-weight:600;text-align:left;
  padding:11px 14px;background:#FBFAF8;border-bottom:1px solid var(--line-2);white-space:nowrap}
tbody td{padding:12px 14px;border-bottom:1px solid var(--line-2);vertical-align:middle;font-size:13px}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#FBFAF7}
.t{font-variant-numeric:tabular-nums;color:var(--ink-2);white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.taskcell{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.proj{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2);font-size:12.5px}
.badge{display:inline-block;font-size:11.5px;font-weight:640;padding:3.5px 8px;border-radius:999px;letter-spacing:.01em}
.b-dsh{color:var(--accent);background:var(--accent-soft)}
.b-pi{color:var(--pi);background:var(--pi-soft)}
.b-rpc{color:var(--rpc);background:var(--rpc-soft)}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:3.5px 9px;border-radius:999px}
.pill:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.85}
.p-run{color:var(--run);background:var(--run-soft)}
.p-ok{color:var(--run);background:var(--run-soft)}
.p-warn{color:var(--warn);background:var(--warn-soft)}
.p-bad{color:var(--bad);background:var(--bad-soft)}
.tokcell{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.toknum{font-variant-numeric:tabular-nums;color:var(--ink-2);font-size:12.5px;min-width:42px;text-align:right}
.bar{width:46px;height:4px;border-radius:99px;background:#EFEDE8;overflow:hidden;flex:none}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#818CF8,#4338CA)}
.money{font-variant-numeric:tabular-nums;color:var(--ink);white-space:nowrap}
code.k{font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-3)}
.foot{display:flex;justify-content:space-between;gap:16px;color:var(--ink-3);font-size:11.5px;margin-top:14px;flex-wrap:wrap}
.empty{padding:26px 20px;color:var(--ink-3);font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
.grid1{margin-bottom:18px}
/* 表格/图表一律在容器内部横滚：整页被撑宽会让手机浏览器把布局视口放大到 ~750px，
   页面整体缩小、右侧像被裁掉。这条必须无条件生效——它得在首次布局前就挡住表格溢出，
   否则写在媒体查询里永远来不及（视口已经被撑大了）。 */
.tscroll{overflow-x:auto;overscroll-behavior-x:contain}
.grid2>*,.cards>*{min-width:0}
.chart{padding:12px 14px 10px;overflow-x:auto;overscroll-behavior-x:contain}
.chart svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--ink-2);padding:9px 18px 0}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{width:9px;height:9px;display:inline-block;border-radius:2px}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
@media (max-width:860px){.cards{grid-template-columns:repeat(2,1fr)}body{padding:22px 16px 32px}h1{font-size:23px}.task{max-width:180px}}
/* 手机竖屏：表格/图表在面板内部横滚，页面本身绝不横向溢出
   （整页被撑宽会让移动浏览器把布局视口放大到 ~750px，所有字一起变小） */
@media (max-width:700px){
  body{padding:16px 12px 30px}
  .head{flex-direction:column;align-items:flex-start;gap:10px;margin-bottom:16px}
  h1{font-size:22px}
  .sub{font-size:12.5px}
  .stamp{text-align:left;white-space:normal}
  .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .card{padding:12px 13px 13px;border-radius:12px}
  .card .v{font-size:21px}
  .panel{border-radius:14px}
  .panel+.panel{margin-top:14px}
  .panel-h{padding:12px 13px;gap:8px;flex-wrap:wrap}
  .grid2,.grid1{gap:14px;margin:14px 0}
  .live{padding:14px 13px 14px}
  .live-title{font-size:16px}
  .timeline li{grid-template-columns:48px 62px minmax(0,1fr);gap:7px;font-size:12px}
  .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}
  .tscroll table{min-width:720px}
  .tscroll table.slices{min-width:640px}
  .chart{padding:9px 9px 7px;overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;
    background-image:radial-gradient(farthest-side at 100% 50%,rgba(20,22,28,.26),rgba(20,22,28,0));
    background-position:right center;background-size:16px 100%;background-repeat:no-repeat}
  .chart svg{min-width:500px}
  .chart svg.net{min-width:760px}
  .legend{font-size:10.5px;gap:10px;padding:8px 12px 0}
  .ctl{gap:8px}
  .pctl a,.ctl a{padding:6px 8px}
  .foot{font-size:10.5px;gap:8px}
}
"""


def render(recs, refresh):
    LANG["v"] = "zh"          # clean 视图固定中文；避免复用进程时沿用上一次的 en
    now = datetime.now(TZ)
    today = now.date()
    running = [r for r in recs if r["state"] == "RUNNING"]
    today_recs = [r for r in recs if datetime.fromtimestamp(r["start"], TZ).date() == today]

    tok_today = sum(r.get("tokens") or 0 for r in today_recs)
    cny_today = sum(r.get("cost_cny") or 0 for r in today_recs if r["worker"] == "DSH")
    usd_today = sum(r.get("cost_usd") or 0 for r in today_recs if r["worker"] != "DSH")

    parts = []
    parts.append(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>执行者看板 · DSH / Pi</title>
{'<meta http-equiv="refresh" content="%d">' % refresh if refresh else ''}
<style>{CSS}</style></head><body><div class="wrap">""")

    parts.append(f"""
<div class="eyebrow">Execution log</div>
<div class="head">
  <div>
    <h1>执行者看板</h1>
    <div class="sub">我派给 DSH / Pi 的任务，按真实会话事件流汇总</div>
  </div>
  <div class="stamp"><b>{now:%m-%d %H:%M:%S}</b><br>{'每 %d 秒自动刷新' % refresh if refresh else '静态快照'}</div>
</div>

<div class="cards">
  <div class="card"><div class="k">正在运行</div><div class="v">{len(running)}<small>个</small></div></div>
  <div class="card"><div class="k">今天跑了</div><div class="v">{len(today_recs)}<small>次</small></div></div>
  <div class="card"><div class="k">今天 Tokens</div><div class="v">{human_tok(tok_today)}</div></div>
  <div class="card"><div class="k">今天花费</div><div class="v">≈¥{cny_today:.2f}<small>{('另 Pi $%.3f' % usd_today) if usd_today else 'Pi 未产生费用'}</small></div></div>
</div>
""")

    # 正在跑
    if running:
        r = running[0]
        calls = (r.get("tool_calls") or [])[-7:]
        tl = "".join(
            f"<li><time>{datetime.fromtimestamp(ts, TZ):%H:%M:%S}</time>"
            f"<span class='tool'>{e(name)}</span><span class='arg'>{e(arg[:150])}</span></li>"
            for ts, name, arg in calls
        ) or f"<li><span class='arg'>{T('no_tools')}</span></li>"
        parts.append(f"""
<div class="panel">
  <div class="panel-h"><h2>正在运行</h2><span class="hint">工具调用时间线（最近 {len(calls)} 条）</span></div>
  <div class="live">
    <div class="live-top">
      <div>
        <div class="live-meta" style="margin-bottom:9px"><span><i class="dot"></i>运行中</span><span class="sep">·</span><span>{e(r.get('project'))}</span></div>
        <p class="live-title">{e(clean_title(r.get('label')))}</p>
        <div class="live-meta"><span>{human_tok(r.get('tokens'))} tokens</span><span class="sep">·</span><span>{cost_text(r)}</span><span class="sep">·</span><span>会话 <code class="k">{e(r['key'])}</code></span></div>
      </div>
      <span class="chip">{dur_text(r)}</span>
    </div>
    <ul class="timeline">{tl}</ul>
  </div>
</div>""")

    # 历史表
    rows = []
    maxtok = max([r.get("tokens") or 0 for r in recs] + [1])
    for r in recs[:26]:
        st = r["state"]
        cls = STATE_CLASS.get(st, "ok")
        worker = r.get("worker", "")
        bcls = "b-dsh" if worker == "DSH" else ("b-rpc" if "rpc" in worker else "b-pi")
        pct = int(100 * (r.get("tokens") or 0) / maxtok)
        rows.append(f"""<tr>
  <td class="t">{datetime.fromtimestamp(r['start'], TZ):%m-%d %H:%M}</td>
  <td><span class="badge {bcls}">{e(worker)}</span></td>
  <td><span class="proj" title="{e(r.get('project'))}">{e(r.get('project'))}</span></td>
  <td><span class="taskcell" title="{e(clean_title(r.get('label')))} · {e(r['key'])}">{e(clean_title(r.get('label')))}</span></td>
  <td><span class="pill p-{cls}">{e(STATE_LABEL.get(st, st))}</span></td>
  <td class="num"><div class="tokcell"><span class="toknum">{human_tok(r.get('tokens'))}</span><span class="bar"><i style="width:{max(pct,3)}%"></i></span></div></td>
  <td class="num money">{cost_text(r)}</td>
  <td class="num t">{dur_text(r)}</td>
</tr>""")

    parts.append(charts_html(recs) + network_block(recs))

    parts.append(f"""
<div class="panel">
  <div class="panel-h"><h2>最近运行</h2><span class="hint">共 {len(recs)} 条 · 显示最近 26 条</span></div>
  <div class="tscroll"><table>
    <colgroup><col style="width:90px"><col style="width:86px"><col style="width:148px"><col><col style="width:90px"><col style="width:152px"><col style="width:86px"><col style="width:78px"></colgroup>
    <thead><tr><th>时间</th><th>执行者</th><th>项目</th><th>任务</th><th>状态</th><th>Tokens</th><th>花费</th><th>耗时</th></tr></thead>
  <tbody>{''.join(rows) if rows else '<tr><td colspan="8" class="empty">还没有记录</td></tr>'}</tbody></table></div>
</div>

<div class="foot">
  <span>数据源：~/.dsh/sessions（zstd JSONL 事件流）· ~/.pi/agent/sessions · ~/.hermes/logs/pi-rpc-*.jsonl</span>
  <span>明细 <code class="k">worker_runs.py show &lt;KEY&gt;</code> · Token 条按最大值归一 · ¥ 为按 deepseek-flash 官方价估算，$ 为 pi 自报</span>
</div>
</div>{MOBILE_JS}</body></html>""")
    return "".join(parts)


# --------------------------------------------------------------- 像素风渲染（方案 B）

FONT_TTF_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".hermes", "cache", "fonts", "pressstart2p.ttf"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "PressStart2P-Regular.ttf"),
]
FONT_TTF = next((p for p in FONT_TTF_CANDIDATES if os.path.exists(p)), FONT_TTF_CANDIDATES[0])
PIXEL_CACHE = os.path.join(os.path.dirname(CACHE), "worker-dashboard-pixel.html")

SPRITES = {
    "run": ["#.......", "##......", "###.....", "####....", "#####...", "####....", "###.....", "##......"],
    "stack": ["########", "........", "########", "........", "########", "........", "........", "........"],
    "chip": ["..####..", ".#....#.", "#.####.#", "#.#..#.#", "#.#..#.#", "#.####.#", ".#....#.", "..####.."],
    "coin": ["#.....#.", "#.....#.", ".#...#..", "..#.#...", "#######.", "..#.#...", "#######.", "...#...."],
}


def _font_css():
    import base64
    try:
        with open(FONT_TTF, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError:
        return ""
    return ("@font-face{font-family:'PS2P';font-style:normal;font-weight:400;font-display:swap;"
            f"src:url(data:font/ttf;base64,{b64}) format('truetype')}}")


def sprite(name):
    rows = SPRITES.get(name) or SPRITES["stack"]
    rects = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(rows) for x, ch in enumerate(row) if ch == "#"
    )
    return f'<svg class="sp" viewBox="0 0 8 8" fill="currentColor" aria-hidden="true">{rects}</svg>'


def dur_lcd(r):
    end = r.get("end") or time.time()
    sec = int(max(0, end - (r.get("start") or end)))
    if sec < 3600:
        return f"{sec // 60}:{sec % 60:02d}"
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def seg_bar(pct, cells=12):
    on = max(1, round(cells * max(pct, 1) / 100))
    return "".join(f"<i class='{'on' if i < on else ''}'></i>" for i in range(cells))


CSS_PIXEL = """
:root{
  --paper:#F2EEE3; --panel:#FFFCF3; --ink:#141519; --ink-2:#4E5048; --ink-3:#6E7066;
  --line:#141519; --hair:#DED7C6; --grid:rgba(20,21,25,.055);
  --red:#C7413B; --green:#2C8C57; --amber:#A9710A; --blue:#2B5F8A; --violet:#5A4B8C;
  --green-soft:#E4F1E6; --red-soft:#FAE5E3; --amber-soft:#FAF0DA; --violet-soft:#EAE5F5;
}
*{box-sizing:border-box}
body{margin:0;padding:36px 30px 46px;background:var(--paper);color:var(--ink);
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:16px 16px;
  font:13.5px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;
  -webkit-font-smoothing:antialiased}
body:after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
  background:repeating-linear-gradient(to bottom,rgba(20,21,25,.028) 0 1px,transparent 1px 4px)}
.wrap{max-width:1080px;margin:0 auto;position:relative;z-index:1}
.px{font-family:'PS2P',ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:400;letter-spacing:0}
.sp{width:17px;height:17px;shape-rendering:crispEdges;vertical-align:-4px;color:var(--ink-2)}

.top{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:24px}
.kicker{font-size:9px;letter-spacing:.08em;color:var(--ink-2);text-transform:uppercase;
  display:inline-flex;align-items:center;gap:8px;margin-bottom:14px}
.kicker b{background:var(--ink);color:var(--paper);padding:4px 7px 3px;font-size:8.5px}
h1{font-size:30px;line-height:1.1;letter-spacing:-.02em;margin:0 0 13px;font-weight:800}
h1 .rule{display:block;width:148px;height:4px;margin-top:12px;
  background:repeating-linear-gradient(90deg,var(--ink) 0 7px,transparent 7px 13px)}
.sub{color:var(--ink-2);font-size:13px}
.clock{border:2px solid var(--line);background:var(--panel);box-shadow:4px 4px 0 var(--line);
  padding:9px 13px 10px;min-width:172px}
.clock .lbl{font-size:8px;color:var(--ink-3);margin-bottom:7px}
.lcd{background:#EDE6D1;border:1.5px solid var(--line);box-shadow:inset 2px 2px 0 rgba(20,21,25,.16);padding:9px 11px 7px}
.clock .t{font-size:17px;line-height:1;color:#1F6B42}
.clock .d{font-size:8px;color:var(--ink-2);margin-top:10px;display:flex;justify-content:space-between;align-items:center}
.blink{display:inline-block;width:7px;height:11px;background:var(--green);animation:bl 1.15s steps(1) infinite}
@keyframes bl{0%,49%{opacity:1}50%,100%{opacity:0}}

.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat{background:var(--panel);border:2px solid var(--line);box-shadow:4px 4px 0 var(--line);padding:13px 14px 15px}
.stat .row1{display:flex;align-items:center;justify-content:space-between;color:var(--ink-2)}
.stat .k{font-size:8px;letter-spacing:.05em}
.stat .v{font-size:22px;line-height:1;margin:0;letter-spacing:-.01em;color:#111}
.stat .lcd{margin:13px 0 10px}
.stat .c{font-size:11.5px;color:var(--ink-2)}
.stat.hot{border-color:var(--ink);background:#FFF8E6}
.stat .c2{display:flex;justify-content:space-between;gap:6px;font-size:10px;color:var(--ink-2);
  font-variant-numeric:tabular-nums;margin-top:6px;letter-spacing:-.01em;white-space:nowrap}
.stat .c2 b{font-weight:600;color:#33342E}
.stat .c2.dim{color:var(--ink-3);font-size:10px;margin-top:4px;justify-content:flex-start}

.panel{background:var(--panel);border:2px solid var(--line);box-shadow:5px 5px 0 var(--line)}
.panel+.panel{margin-top:22px}
.panel-h{display:flex;align-items:center;gap:11px;padding:11px 16px;border-bottom:2px solid var(--line);background:#F8F4E7}
.panel-h h2{font-size:9px;margin:0;letter-spacing:.06em;text-transform:uppercase}
.panel-h .sq{width:9px;height:9px;background:var(--green);animation:bl 1.15s steps(1) infinite}
.panel-h .right{margin-left:auto;display:flex;gap:9px;align-items:center}
.tag{font-size:8px;padding:5px 8px 4px;border:1.5px solid var(--line);background:var(--panel);color:var(--ink-2)}
.tag.live{background:var(--green-soft);color:var(--green);border-color:var(--green);font-weight:400}

.live{padding:18px 18px 20px}
.live .task{font-size:20px;font-weight:760;letter-spacing:-.018em;line-height:1.3;margin:0 0 9px;max-width:800px}
.live .meta{font-size:11.5px;color:var(--ink-2);display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.live .meta b{color:#C9C2B0;font-weight:400}
.live .meta .led{display:inline-flex;align-items:center;gap:6px;color:var(--green);font-family:'PS2P',monospace;font-size:8.5px}
.live .meta .led i{width:7px;height:7px;background:var(--green);display:inline-block;animation:bl 1.15s steps(1) infinite}
.live .meta .id{font-family:'PS2P',monospace;font-size:8.5px;color:var(--ink-3)}
.timeline{list-style:none;margin:0;padding:0;border-top:1.5px dashed var(--hair)}
.timeline li{display:grid;grid-template-columns:66px 92px minmax(0,1fr);gap:11px;align-items:center;
  padding:10px 0;border-bottom:1.5px dashed var(--hair);font-size:13px;color:var(--ink)}
.timeline li:last-child{border-bottom:0;padding-bottom:2px}
.timeline time{font-family:'PS2P',monospace;font-size:9.5px;color:var(--ink-3)}
.tool{font-family:'PS2P',monospace;font-size:8px;text-align:center;padding:5px 4px 4px;border:1.5px solid var(--line);
  background:#EEF3F8;color:var(--blue);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.arg{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#33342E}

table{width:100%;border-collapse:collapse;table-layout:fixed}
thead th{font-family:'PS2P',monospace;font-weight:400;font-size:8px;letter-spacing:.04em;color:var(--ink-2);
  text-align:left;padding:11px 13px;background:#F8F4E7;border-bottom:2px solid var(--line);white-space:nowrap}
tbody td{padding:11px 13px;border-bottom:1.5px dashed var(--hair);vertical-align:middle;font-size:13px}
tbody tr:hover{background:#FBF7EA}
.t{font-family:'PS2P',monospace;font-size:8.5px;color:var(--ink-2);white-space:nowrap}
.num{text-align:right;white-space:nowrap}
.taskcell{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink);font-weight:550}
.proj{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2);font-size:12.5px}
.badge{font-family:'PS2P',monospace;font-size:8px;padding:5px 6px 4px;border:1.5px solid currentColor;display:inline-block}
.b-dsh{color:var(--violet);background:var(--violet-soft)}
.b-pi{color:var(--blue);background:#E8F1F6}
.b-rpc{color:var(--amber);background:var(--amber-soft)}
.b-hermes{color:#F2EEE3;background:#23242A}
.b-sub{color:#B0472F;background:#F7E4DE}
.b-codex{color:#157A5B;background:#DFF0E9}
.b-claude{color:#9C2A6B;background:#F6E3EF}
.b-cron{background:var(--amber-soft);color:var(--amber)}
.pill{font-family:'PS2P',monospace;font-size:8px;padding:5px 7px 4px;border:1.5px solid currentColor;display:inline-block;white-space:nowrap}
.p-run{color:var(--green);background:var(--green-soft)}
.p-ok{color:var(--ink-2);background:#F1EEE4}
.p-warn{color:var(--amber);background:var(--amber-soft)}
.p-bad{color:var(--red);background:var(--red-soft)}
.tokcell{display:flex;align-items:center;justify-content:flex-end;gap:8px}
.toknum{font-family:'PS2P',monospace;font-size:9px;color:var(--ink-2);min-width:44px;text-align:right}
.seg{display:flex;gap:2px}
.seg i{width:4px;height:9px;background:#E2DBCA}
.seg i.on{background:var(--violet)}
.money{font-family:'PS2P',monospace;font-size:8.5px;color:var(--ink)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin:22px 0}
.grid1{margin:22px 0}
/* 表格/图表必须在容器内部横滚（无条件生效）：整页被撑宽时移动浏览器会把布局视口放大到
   ~750px，页面整体缩小、字全变小；写在媒体查询里来不及，因为视口那时已经被撑大了。 */
.tscroll{overflow-x:auto;overscroll-behavior-x:contain}
.grid2>*,.stats>*{min-width:0}
.chart{padding:12px 14px 10px;overflow-x:auto;overscroll-behavior-x:contain}
.chart svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:10px;color:var(--ink-2);padding:9px 16px 0}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{width:9px;height:9px;display:inline-block;border:1px solid rgba(20,21,25,.5)}
.foot{display:flex;justify-content:space-between;gap:16px;margin-top:16px;color:var(--ink-3);font-size:10.5px;flex-wrap:wrap}
.empty{padding:26px 18px;color:var(--ink-3);font-size:12.5px}
table.slices .num,table.slices .t{font-variant-numeric:tabular-nums}
table.slices tr.req td{background:#F8F4E7;font-weight:640;color:var(--ink-2);border-bottom:1.5px solid var(--hair)}
table.slices tfoot td{padding:9px 13px;color:var(--ink-3);font-size:10.5px;
  border-top:1.5px dashed var(--hair);font-variant-numeric:tabular-nums}
@media (max-width:860px){.grid2{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}body{padding:22px 14px 30px}
  h1{font-size:23px}.top{flex-direction:column}.clock{width:100%}.timeline li{grid-template-columns:54px 76px minmax(0,1fr)}}

/* ---- 手机竖屏（≤700px）：单列 + 面板内横滚 + 触控尺寸放大 ----
   表格/图表必须在容器内部横滚：整页被撑宽时，移动浏览器会把布局视口放大到 ~750px，
   页面整体缩小、字全变小，而且右侧内容看起来被裁掉。 */
@media (max-width:700px){
  body{padding:16px 12px calc(28px + env(safe-area-inset-bottom))}
  .top{gap:13px;margin-bottom:16px}
  .kicker{font-size:8px;margin-bottom:10px}
  h1{font-size:24px;margin-bottom:10px}
  h1 .rule{width:104px;height:3px;margin-top:9px}
  .sub{font-size:12px;line-height:1.5}
  .clock{padding:8px 10px 9px;box-shadow:3px 3px 0 var(--line)}
  .clock .lcd{padding:8px 9px 6px}
  .clock .t{font-size:16px}

  .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:14px}
  .stat{padding:10px 11px 12px;box-shadow:3px 3px 0 var(--line)}
  .stat .k{font-size:7.5px}
  .stat .v{font-size:18px}
  .stat .lcd{margin:9px 0 8px;padding:7px 8px 5px}
  .stat .c{font-size:10.5px;line-height:1.4}
  .stat .c2{white-space:normal;flex-wrap:wrap;gap:1px 8px;font-size:9px;letter-spacing:0}
  .stat .c2.dim{margin-top:3px}

  .panel+.panel{margin-top:14px}
  .grid2,.grid1{gap:14px;margin:14px 0}
  .panel-h{padding:9px 11px;gap:7px;flex-wrap:wrap}
  .panel-h h2{font-size:8px}
  .panel-h .right{margin-left:auto;flex-wrap:wrap;gap:6px;justify-content:flex-end;min-width:0}
  .tag{font-size:7px;padding:4px 6px 3px}

  .live{padding:12px 13px 14px}
  .live .task{font-size:16px;line-height:1.35;margin-bottom:8px}
  .live .meta{font-size:10.5px;gap:7px;margin-bottom:12px}
  .live .meta .id{font-size:7.5px;word-break:break-all}
  .timeline li{grid-template-columns:50px 60px minmax(0,1fr);gap:7px;font-size:11.5px;padding:8px 0}
  .timeline time{font-size:8px}
  .tool{font-size:7px;padding:4px 3px 3px}
  .arg{font-size:11px}

  .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}

  /* 三张表在手机上重排成卡片：隐藏表头，一行拆成三行网格。
     8 列挤在 362px 里必然要横滚，卡片式更好读，也省掉"右边还有内容"的猜谜。 */
  table.hist,table.proc,table.slices{display:block;width:100%}
  table.hist colgroup,table.proc colgroup,table.slices colgroup{display:none}
  table.hist thead,table.proc thead,table.slices thead{display:none}
  table.hist tbody,table.proc tbody,table.slices tbody{display:block}
  table.hist tbody tr,table.proc tbody tr{display:grid;gap:6px 11px;align-items:center;
    padding:10px 12px;border-bottom:1.5px dashed var(--hair)}
  table.hist tbody tr:last-child,table.proc tbody tr:last-child{border-bottom:0}
  table.hist tbody td,table.proc tbody td,table.slices tbody td{padding:0;border:0;display:block;min-width:0}
  table.hist tbody tr{grid-template-columns:minmax(0,1fr) auto auto auto;
    grid-template-areas:"time worker state state" "task task task task" "proj toks cost dur"}
  table.hist tbody .seg{display:none}
  table.hist tbody td:nth-child(1){grid-area:time;white-space:normal;overflow-wrap:anywhere}
  table.hist tbody td:nth-child(2){grid-area:worker}
  table.hist tbody td:nth-child(3){grid-area:proj}
  table.hist tbody td:nth-child(4){grid-area:task}
  table.hist tbody td:nth-child(5){grid-area:state;justify-self:end}
  table.hist tbody td:nth-child(6){grid-area:toks;justify-self:end}
  table.hist tbody td:nth-child(7){grid-area:cost;justify-self:end}
  table.hist tbody td:nth-child(8){grid-area:dur;justify-self:end}
  table.hist tbody .taskcell{white-space:normal;overflow:hidden;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:12px;line-height:1.35}
  table.hist tbody td:nth-child(6):before{content:"TOK ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}
  table.hist tbody td:nth-child(8):before{content:"DUR ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}

  table.proc tbody tr{grid-template-columns:auto auto minmax(0,1fr);
    grid-template-areas:"pid agent elapsed" "proj proj cpu" "task task task"}
  table.proc tbody td:nth-child(1){grid-area:pid}
  table.proc tbody td:nth-child(2){grid-area:agent}
  table.proc tbody td:nth-child(3){grid-area:proj}
  table.proc tbody td:nth-child(4){grid-area:elapsed;justify-self:start}
  table.proc tbody td:nth-child(5){grid-area:cpu;justify-self:end}
  table.proc tbody td:nth-child(6){grid-area:task}
  table.proc tbody td:nth-child(4):before{content:"DUR ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}
  table.proc tbody td:nth-child(5):before{content:"CPU ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}
  table.proc tbody .taskcell{white-space:normal;font-size:11.5px;line-height:1.35}

  table.slices tbody tr.sl{display:grid;gap:5px 9px;align-items:center;
    padding:10px 12px;border-bottom:1.5px dashed var(--hair);
    grid-template-columns:auto minmax(0,1fr) auto;
    grid-template-areas:"state worker updated" "goal goal goal" "att verdict runs"}
  table.slices tbody tr.sl td:nth-child(1){grid-area:state}
  table.slices tbody tr.sl td:nth-child(2){grid-area:goal}
  table.slices tbody tr.sl td:nth-child(3){grid-area:worker;justify-self:start}
  table.slices tbody tr.sl td:nth-child(4){grid-area:att}
  table.slices tbody tr.sl td:nth-child(5){grid-area:verdict}
  table.slices tbody tr.sl td:nth-child(6){grid-area:runs;justify-self:end}
  table.slices tbody tr.sl td:nth-child(7){grid-area:updated;justify-self:end}
  table.slices tbody tr.sl td:nth-child(4):before{content:"ATT ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}
  table.slices tbody tr.sl td:nth-child(6):before{content:"RUN ";font-family:'PS2P',monospace;font-size:7px;color:var(--ink-3)}
  table.slices tbody tr.sl .taskcell{white-space:normal;overflow:visible;font-size:11.5px;line-height:1.35}
  table.slices tbody tr.req{display:block;padding:9px 12px;border-bottom:1.5px solid var(--hair)}
  table.slices tbody tr.req td{display:block;padding:0;border:0;font-size:11px;line-height:1.35}
  table.slices tfoot{display:block}
  table.slices tfoot td{display:block;padding:9px 12px;border-top:0}

  .chart{padding:9px 9px 7px;overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;
    background-image:radial-gradient(farthest-side at 100% 50%,rgba(20,21,25,.38),rgba(20,21,25,0));
    background-position:right center;background-size:16px 100%;background-repeat:no-repeat}
  .chart svg{min-width:520px}
  .chart svg.net{min-width:760px}
  .legend{font-size:9px;gap:10px;padding:8px 11px 0}

  .pctl{gap:5px}
  .pctl a{min-height:30px;display:inline-flex;align-items:center;padding:0 9px;font-size:8px}
  .ctl{gap:8px;font-size:7.5px}
  .ctl a{padding:8px 9px;font-size:8px;min-height:30px;display:inline-flex;align-items:center}
  .foot{font-size:9px;gap:8px;line-height:1.5}
  .foot .px{overflow-wrap:anywhere}
  .empty{padding:18px 12px;font-size:11.5px}
}

@media (max-width:380px){
  .stats{grid-template-columns:minmax(0,1fr)}
  .timeline li{grid-template-columns:46px 54px minmax(0,1fr)}
  .chart svg{min-width:460px}
}
"""

PIXEL_STATE = {"RUNNING": "p-run", "done": "p-ok", "interrupted": "p-warn",
               "aborted": "p-warn", "FAILED": "p-bad"}
STATE_KEY = {"RUNNING": "st_run", "done": "st_done", "interrupted": "st_halt",
             "aborted": "st_abort", "FAILED": "st_fail"}


def state_label(st):
    return T(STATE_KEY.get(st, "st_done"))
PIXEL_WORKER = {"DSH": "b-dsh", "Pi": "b-pi", "Pi·rpc": "b-rpc",
                "Hermes": "b-hermes", "Hermes·sub": "b-sub", "Codex": "b-codex", "Claude": "b-claude"}
WORKER_ORDER = ["DSH", "Pi", "Pi·rpc", "Hermes", "Hermes·sub", "Codex", "Claude"]
PIX_HERMES = "#3E4E66"
PIX_SUB = "#B0472F"
PIX_CODEX = "#157A5B"
PIX_CLAUDE = "#9C2A6B"


def worker_color(w):
    return {"DSH": PIX_DSH, "Pi": PIX_PI, "Pi·rpc": PIX_RPC,
            "Hermes": PIX_HERMES, "Hermes·sub": PIX_SUB, "Codex": PIX_CODEX,
            "Claude": PIX_CLAUDE}.get(w, PIX_PI)


def worker_badge(w):
    return PIXEL_WORKER.get(w, "b-pi")
WD = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WD_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_label(wd):
    return WD_ZH[wd] if LANG["v"] == "zh" else WD[wd]



# ------------------------------------------------------------------ 图表（纯 SVG，无外部依赖）

PIX_CACHE = "#C7B9E8"
PIX_IN = "#6C55A8"
PIX_OUT = "#23242A"
PIX_DSH = "#5A4B8C"
PIX_PI = "#0E7C86"
PIX_RPC = "#B5820E"
PIX_OK = "#2C8C57"
PIX_RUN = "#1F6FB2"
PIX_HALT = "#A9710A"
PIX_BAD = "#C7413B"
PIX_GRID = "#D9D2BF"
PIX_AXIS = "#5E6057"


def _state_color(st):
    """状态 → 颜色。RUNNING 必须有自己的颜色：以前落到 PIX_BAD，
    在跑的任务和失败的任务一个颜色，看图就分不出来。"""
    if st == "done":
        return PIX_OK
    if st == "RUNNING":
        return PIX_RUN
    if st in ("interrupted", "aborted", "cancelled"):
        return PIX_HALT
    return PIX_BAD


def _t(x, y, txt, size=7.0, fill=PIX_AXIS, anchor="start"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="PS2P,monospace" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}">{e(txt)}</text>')


def _nice(v):
    if v <= 0:
        return 1.0
    import math
    exp = 10 ** math.floor(math.log10(v))
    m = v / exp
    for k in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m <= k + 1e-9:
            return k * exp
    return 10 * exp


def _tip(txt):
    return f"<title>{e(txt)}</title>"


def _tok(v):
    if v <= 0:
        return "0"
    if v < 1000:
        return str(int(v))
    if v < 1_000_000:
        return f"{v / 1000:.0f}K"
    return f"{v / 1e6:.1f}M"


def _panel(title, hint, body, legend="", ctl=""):
    return f"""
<div class="panel">
  <div class="panel-h"><i class="sq" style="background:var(--violet);animation:none"></i>
    <h2 class="px">{e(title)}</h2><div class="right">{ctl}<span class="tag px">{e(hint)}</span></div></div>
  {f'<div class="legend">{legend}</div>' if legend else ''}
  <div class="chart">{body}</div>
</div>"""


def chart_daily(recs, days=14, w=520, h=200):
    now = datetime.now(TZ)
    keys = [(now - timedelta(days=i)).date() for i in range(days - 1, -1, -1)]
    agg = {d: [0, 0, 0, 0.0] for d in keys}
    for r in recs:
        d = datetime.fromtimestamp(r["start"], TZ).date()
        if d in agg:
            agg[d][0] += r.get("tok_cache") or 0
            agg[d][1] += r.get("tok_in") or 0
            agg[d][2] += r.get("tok_out") or 0
            agg[d][3] += rec_cny(r)
    maxv = _nice(max([sum(v[:3]) for v in agg.values()] + [1]))
    maxc = _nice(max([v[3] for v in agg.values()] + [0.0001]))
    pl, pr, pt, pb = 60, 58, 12, 24
    pw, ph = w - pl - pr, h - pt - pb
    out = []
    for i in range(5):
        y = pt + ph - ph * i / 4
        out.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="{PIX_GRID}" stroke-width="1" stroke-dasharray="2 3"/>')
        out.append(_t(pl - 6, y + 2.5, _tok(maxv * i / 4), 7, PIX_AXIS, "end"))
    # 右轴：花费
    for i in (0, 2, 4):
        y = pt + ph - ph * i / 4
        out.append(_t(w - pr + 8, y + 2.5, f"{maxc * i / 4:.0f}" if maxc >= 10 else f"{maxc * i / 4:.1f}", 7, PIX_OK))
    slot = pw / days
    bw = max(6, slot * 0.52)
    cost_pts = []
    for i, d in enumerate(keys):
        cx = pl + slot * (i + 0.5)
        x = cx - bw / 2
        y = pt + ph
        for v, col, nm in ((agg[d][0], PIX_CACHE, "cache"), (agg[d][1], PIX_IN, "input"), (agg[d][2], PIX_OUT, "output")):
            if v <= 0:
                continue
            bh = ph * v / maxv
            out.append(f'<rect x="{x:.1f}" y="{y - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}">'
                       f'{_tip(f"{d:%m/%d} {nm} {v:,} tokens")}</rect>')
            y -= bh
        total = sum(agg[d][:3])
        if total:
            out.append(_tip(f"{d:%m/%d} {T('total')} {total:,} tok · ¥{agg[d][3]:.2f}"))
        cost_pts.append((cx, pt + ph - ph * agg[d][3] / maxc))
        if (i % 2 == 0 and i != days - 2) or i == days - 1:
            out.append(_t(cx, h - 8, f"{d:%m/%d}", 7, PIX_AXIS, "middle"))
    if len(cost_pts) > 1:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in cost_pts)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{PIX_OK}" stroke-width="1.6"/>')
        for (x, y), d in zip(cost_pts, keys):
            out.append(f'<rect x="{x - 1.5:.1f}" y="{y - 1.5:.1f}" width="3" height="3" fill="{PIX_OK}">'
                       f'{_tip(f"{d:%m/%d} cost ¥{agg[d][3]:.2f}")}</rect>')
    legend = (f'<span><i style="background:%s"></i>{T("lg_cache")}</span>'
              f'<span><i style="background:%s"></i>{T("lg_input")}</span>'
              f'<span><i style="background:%s"></i>{T("lg_output")}</span>'
              f'<span><i style="background:%s"></i>{T("cost_axis")}</span>') % (PIX_CACHE, PIX_IN, PIX_OUT, PIX_OK)
    svg = f'<svg viewBox="0 0 {w} {h}">{"".join(out)}</svg>'
    return _panel(T("p_tokens", days), T("max_label", _tok(maxv)), svg, legend,
                  _ctl_grp("days", [(7, "7D"), (14, "14D"), (30, "30D")], days))


def chart_gantt(recs, days=7, scope="all", w=520):
    """时间线：一天一行，行内按真实并发分道（lane packing）。

    旧版把同一天的会话全画在一条水平线上，后画的覆盖先画的 —— 十几个会话叠成一条黑条，
    既看不出几次也看不出谁。现在同一天不重叠的会话排同一道，重叠的往下加道。
    """
    now = datetime.now(TZ)
    keys = [(now - timedelta(days=i)).date() for i in range(days - 1, -1, -1)]
    rows = {d: [] for d in keys}
    for r in recs:
        if scope == "dg" and r.get("worker") == "Hermes":
            continue
        segs = r.get("segs") or [[r["start"], r.get("end") or time.time()]]
        for a, b in segs:
            if b <= a:
                b = a + 45
            cur = a
            while True:                                  # 跨零点的一段按天切开
                d = datetime.fromtimestamp(cur, TZ).date()
                midnight = datetime.combine(d, datetime.min.time(), tzinfo=TZ).timestamp()
                if d in rows:
                    s0 = max(0.0, min(86390.0, cur - midnight))
                    s1 = max(s0 + 45, min(86400.0, b - midnight))
                    rows[d].append((s0, s1, r))
                if b <= midnight + 86400:
                    break
                cur = midnight + 86400

    lane_h, lane_gap, row_pad = 5.0, 1.4, 9.0
    layout, row_h = {}, {}
    for d in keys:
        lanes, placed = [], []
        for s0, s1, r in sorted(rows[d], key=lambda x: (x[0], x[1])):
            for i, le in enumerate(lanes):
                if s0 >= le:
                    lanes[i] = s1
                    placed.append((i, s0, s1, r))
                    break
            else:
                lanes.append(s1)
                placed.append((len(lanes) - 1, s0, s1, r))
        layout[d] = (max(1, len(lanes)), placed)
        row_h[d] = max(18.0, len(lanes) * (lane_h + lane_gap) + row_pad)

    pl, pr, pt, pb = 66, 14, 10, 24
    h = pt + sum(row_h.values()) + pb
    pw = w - pl - pr
    out = []
    for hh in range(0, 25, 6):
        x = pl + pw * hh / 24
        out.append(f'<line x1="{x:.1f}" y1="{pt}" x2="{x:.1f}" y2="{h - pb}" '
                   f'stroke="{PIX_GRID}" stroke-width="1" stroke-dasharray="2 3"/>')
        out.append(_t(x, h - 9, str(hh), 7, PIX_AXIS, "middle"))

    y0 = pt
    n_runs, max_lane, seen = 0, 1, set()
    for d in keys:
        nlanes, placed = layout[d]
        max_lane = max(max_lane, nlanes)
        out.append(f'<line x1="{pl}" y1="{y0:.1f}" x2="{w - pr}" y2="{y0:.1f}" '
                   f'stroke="#DED7C6" stroke-width="1"/>')
        out.append(_t(pl - 8, y0 + 9.5, f"{d:%m/%d}", 7.5, PIX_AXIS, "end"))
        if placed:
            runs = {(id(r), r["key"]) for _, _, _, r in placed}
            out.append(_t(pl - 8, y0 + 19, T("tl_segs", len(placed)), 6, "#9A9280", "end"))
            seen |= runs
        for lane, s0, s1, r in placed:
            bx = pl + pw * s0 / 86400
            bw = max(2.6, min(pw * (s1 - s0) / 86400, w - pr - bx))
            by = y0 + 4 + lane * (lane_h + lane_gap)
            col = worker_color(r["worker"])
            op = 0.42 if r["state"] in ("interrupted", "aborted") else 1
            mid = datetime.combine(d, datetime.min.time(), tzinfo=TZ)
            t_a = (mid + timedelta(seconds=s0)).strftime("%H:%M")
            t_b = (mid + timedelta(seconds=s1)).strftime("%H:%M")
            tip = (f"{d:%m/%d} {t_a}→{t_b} · {r['worker']} · {state_label(r['state'])}"
                   f" · {clean_title(r.get('label'))[:46]}"
                   f" · {T('active_tip', dur_text(r), len(r.get('segs') or []) or 1)} · {cost_text(r)}")
            out.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{lane_h}" '
                       f'fill="{col}" fill-opacity="{op}" stroke="#141519" stroke-width="0.35" '
                       f'stroke-opacity="0.45">{_tip(tip)}</rect>')
        y0 += row_h[d]
    out.append(f'<line x1="{pl}" y1="{y0:.1f}" x2="{w - pr}" y2="{y0:.1f}" '
               f'stroke="#DED7C6" stroke-width="1"/>')
    n_runs = len(seen)

    cnt, counted = {}, set()
    for d in keys:
        for _, _, _, r in layout[d][1]:
            k = (id(r), r.get("key"))
            if k in counted:
                continue
            counted.add(k)
            cnt[r["worker"]] = cnt.get(r["worker"], 0) + 1
    legend = "".join(
        f'<span><i style="background:{worker_color(wk)}"></i>{wk} <b>{cnt[wk]}</b></span>'
        for wk in WORKER_ORDER if cnt.get(wk))
    legend += f'<span><i style="background:#B9B2A0"></i>{T("dim_halt")}</span>'
    svg = f'<svg viewBox="0 0 {w} {h:.0f}">{"".join(out)}</svg>'
    ctl = (_ctl_grp("scope", [("all", T("sc_all")), ("dg", T("sc_dg"))], scope) +
           _ctl_grp("tdays", [(7, "7D"), (14, "14D"), (30, "30D")], days))
    return _panel(T("p_timeline", days), T("tl_lanes", n_runs, max_lane), svg, legend, ctl)


def chart_projects(recs, w=520, h=200, top=6):
    agg = {}
    for r in recs:
        k = r.get("project") or "?"
        a = agg.setdefault(k, [0, 0])
        a[0] += r.get("tokens") or 0
        a[1] += 1
    items = sorted(agg.items(), key=lambda kv: -kv[1][0])[:top]
    maxv = _nice(max([v[0] for _, v in items] + [1]))
    pl, pr, pt = 10, 104, 34
    pw = w - pl - pr
    rowh = (h - pt - 22) / max(len(items), 1)
    out = []
    out.append(_t(pl, h - 10, "0", 6.5, PIX_AXIS, "start"))
    for i in range(1, 5):
        x = pl + pw * i / 4
        out.append(f'<line x1="{x:.1f}" y1="{pt - 8}" x2="{x:.1f}" y2="{h - 22}" stroke="{PIX_GRID}" stroke-width="1" stroke-dasharray="2 3"/>')
        out.append(_t(x, h - 10, _tok(maxv * i / 4), 6.5, PIX_AXIS, "middle"))
    total_all = sum(v[0] for v in agg.values()) or 1
    for i, (name, (tok, n)) in enumerate(items):
        y = pt + rowh * i
        bh = min(12, rowh * 0.34)
        bw = max(3, pw * tok / maxv)
        share = 100 * tok / total_all
        out.append(f'<rect x="{pl}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{PIX_DSH}">'
                   f'{_tip(f"{name} · {tok:,} tok · {n} runs · {share:.1f}%")}</rect>')
        out.append(f'<rect x="{pl}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="none" stroke="#141519" stroke-width="0.8"/>')
        out.append(_t(pl, y - 5, name[:20], 7.5, "#33342E"))
        out.append(_t(w - pr + 6, y + bh / 2 + 2.5, f"{_tok(tok)}·{n}·{share:.0f}%", 6.5, PIX_AXIS))
    if not items:
        out.append(_t(pl, pt + 14, T("no_data"), 8, PIX_AXIS))
    svg = f'<svg viewBox="0 0 {w} {h}">{"".join(out)}</svg>'
    return _panel(T("p_projects"), T("h_projects"), svg, "",
                  _ctl_grp("projs", [(4, "4"), (6, "6"), (10, "10")], top))


def chart_durations(recs, w=520, h=200, n=16):
    items = list(reversed(recs[:n]))
    vals = []
    for r in items:
        end = r.get("end") or time.time()
        vals.append(max(30, end - r["start"]))
    maxv = max(vals + [60])
    step = 10 * 60 if maxv > 1800 else (5 * 60 if maxv > 600 else 60)
    maxv = max(step * 4, int((maxv + step - 1) // step) * step)
    pl, pr, pt, pb = 44, 14, 14, 34
    pw, ph = w - pl - pr, h - pt - pb
    slot = pw / max(len(vals), 1)
    bw = max(4, slot * 0.54)
    out = []
    for i in range(5):
        y = pt + ph - ph * i / 4
        out.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="{PIX_GRID}" stroke-width="1" stroke-dasharray="2 3"/>')
        out.append(_t(pl - 6, y + 2.5, _dur_label(maxv * i / 4), 7, PIX_AXIS, "end"))
    prev_day = None
    for i, (r, v) in enumerate(zip(items, vals)):
        cx = pl + slot * (i + 0.5)
        st = datetime.fromtimestamp(r["start"], TZ)
        if prev_day is not None and st.date() != prev_day:
            out.append(f'<line x1="{cx - slot / 2:.1f}" y1="{pt}" x2="{cx - slot / 2:.1f}" y2="{pt + ph}" '
                       f'stroke="#B9B2A0" stroke-width="1" stroke-dasharray="1 2"/>')
            out.append(_t(cx - slot / 2 + 3, pt + 8, f"{st:%m/%d}", 6.5, PIX_AXIS))
        prev_day = st.date()
        bh = ph * v / maxv
        col = _state_color(r["state"])
        tip = f"{clean_title(r.get('label'))[:36]} · {r['worker']} · {r['key']} · {r['state']} · {_dur_label(v)}"
        out.append(f'<rect x="{cx - bw / 2:.1f}" y="{pt + ph - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}">{_tip(tip)}</rect>')
        if (i % 2 == 0 and i != len(items) - 2) or i == len(items) - 1:
            out.append(_t(cx, h - 16, f"{st:%H:%M}", 6.5, PIX_AXIS, "middle"))
    legend = (f'<span><i style="background:{PIX_OK}"></i>{T("lg_done")}</span>'
              f'<span><i style="background:{PIX_RUN}"></i>{T("st_run")}</span>'
              f'<span><i style="background:{PIX_HALT}"></i>{T("lg_halt")}</span>'
              f'<span><i style="background:{PIX_BAD}"></i>{T("lg_fail")}</span>'
              f'<span>{T("dur_x")}</span>')
    svg = f'<svg viewBox="0 0 {w} {h}">{"".join(out)}</svg>'
    return _panel(T("p_duration"), f"LAST {len(items)}", svg, legend)


def _dur_label(sec):
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}"




NET_MARK = """
<defs>
<marker id="ar" viewBox="0 0 7 7" refX="6.4" refY="3.5" markerWidth="8.5" markerHeight="8.5" orient="auto">
<path d="M0,0 L7,3.5 L0,7 z" fill="#8B8371"/></marker>
<marker id="arw" viewBox="0 0 7 7" refX="6.4" refY="3.5" markerWidth="8" markerHeight="8" orient="auto">
<path d="M0,0 L7,3.5 L0,7 z" fill="#4E5048"/></marker>
</defs>"""


def _label_of(r):
    lbl = clean_title(r.get("label"))
    if lbl in ("（无标题）", "(无标题)", "无标题"):
        return f"[{r.get('project') or '?'}]"
    return lbl


def chart_network(recs, w=1040, h=560, n=12):
    items = list(reversed(recs[:n]))
    if not items:
        return _panel(T("p_graph"), T("no_data"), "", "")
    projs = []
    for r in items:
        pr = r.get("project") or "?"
        if pr not in projs:
            projs.append(pr)
    workers = []
    for r in items:
        if r["worker"] not in workers:
            workers.append(r["worker"])

    top, bot = 78, h - 50
    n_work = len({r["worker"] for r in items})
    n_proj = len({(r.get("project") or "?") for r in items})
    # 任务卡 28px 高，聚合节点 28px 高：画布不够高就长高，绝不让卡片互相压住
    h = max(h, top + 32 * (len(items) - 1) + 62, top + 34 * max(n_work, n_proj) + 70)
    bot = h - 50
    step = (bot - top) / max(len(items) - 1, 1)
    ty = [top + i * step for i in range(len(items))]

    hub = {"x": 22, "y": h / 2 - 16, "w": 100, "h": 32}
    wx, ww = 176, 104
    tx, tw = 348, 254
    bus = 702
    px, pw_ = 772, 246

    def _spread(groups, gap=34.0):
        """把同一列的聚合节点摊开。

        位置取该组任务的均值是"最像中心"的，但两个组均值接近时会完全重叠
        （Hermes 压在 Pi·rpc 上就是这样）。按目标位置排序后依次顶开，再整体夹回画布内。
        """
        order = sorted(groups.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), str(kv[0])))
        ys = [min(max(sum(v) / len(v), top), bot) for _, v in order]
        for _ in range(3):
            for i in range(1, len(ys)):
                ys[i] = max(ys[i], ys[i - 1] + gap)
            if ys and ys[-1] > bot:
                shift = ys[-1] - bot
                ys = [y - shift for y in ys]
            if ys and ys[0] < top:
                ys[0] = top
        return {order[i][0]: ys[i] for i in range(len(order))}

    wy, py = {}, {}
    for i, r in enumerate(items):
        wy.setdefault(r["worker"], []).append(ty[i])
        py.setdefault(r["project"] or "?", []).append(ty[i])
    wy = {k: v - 14 for k, v in _spread(wy).items()}
    py = {k: v - 14 for k, v in _spread(py).items()}

    out = [NET_MARK]

    def cd(x1, y1, x2, y2):
        dx = (x2 - x1) * 0.55
        return (f"M{x1:.1f},{y1:.1f} C{x1 + dx:.1f},{y1:.1f} "
                f"{x2 - dx:.1f},{y2:.1f} {x2:.1f},{y2:.1f}")

    def edge(a, b, proj, d, color, width=1.3, dashed=False):
        dash = ' stroke-dasharray="2 3"' if dashed else ''
        out.append(f'<g class="eg" data-a="{e(a)}" data-b="{e(b)}" data-proj="{e(proj)}">'
                   f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
                   f'marker-end="url(#arw)" opacity=".85"/></g>')

    # HERMES → worker
    for wk in workers:
        col = worker_color(wk)
        edge("hermes", f"w:{wk}", "", cd(hub["x"] + hub["w"], hub["y"] + hub["h"] / 2, wx, wy[wk] + 14), col, 2.0)

    # worker → task
    for i, r in enumerate(items):
        col = PIX_DSH if r["worker"] == "DSH" else (PIX_PI if "rpc" not in r["worker"] else PIX_RPC)
        edge(f"w:{r['worker']}", f"t:{r['key']}", r.get("project") or "?",
             cd(wx + ww, wy[r["worker"]] + 14, tx, ty[i] + 14), col)

    # task → 项目：先汇到总线，再由总线进项目（避免 7 条虚线交叉）
    for pr in projs:
        ys = sorted(ty[i] + 14 for i, r in enumerate(items) if (r.get("project") or "?") == pr)
        lo, hi = min(ys), max(ys)
        out.append(f'<g class="eg busline" data-a="bus:{e(pr)}" data-b="p:{e(pr)}" data-proj="{e(pr)}">'
                   f'<path d="M{bus},{lo:.1f} L{bus},{hi:.1f}" stroke="#9E9682" stroke-width="1.6" opacity=".95"/>'
                   f'<path d="M{bus},{py[pr] + 14:.1f} L{px - 7},{py[pr] + 14:.1f}" stroke="#9E9682" '
                   f'stroke-width="1.4" marker-end="url(#ar)" opacity=".9"/></g>')

    for i, r in enumerate(items):
        pr = r.get("project") or "?"
        edge(f"t:{r['key']}", f"bus:{pr}", pr,
             f"M{tx + tw},{ty[i] + 14:.1f} L{bus},{ty[i] + 14:.1f}", "#C0B8A4", 1, dashed=True)

    # hub
    out.append(f'<g class="gn" data-nid="hermes">'
               f'<rect x="{hub["x"]}" y="{hub["y"]}" width="{hub["w"]}" height="{hub["h"]}" fill="#141519"/>'
               f'{_t(hub["x"] + hub["w"] / 2, hub["y"] + 19, "HERMES", 9, "#F2EEE3", "middle")}'
               f'{_t(hub["x"] + hub["w"] / 2, hub["y"] - 8, T("orch"), 6.5, PIX_AXIS, "middle")}'
               f'{_t(hub["x"] + hub["w"] / 2, hub["y"] + hub["h"] + 14, T("runs_tag", len(items)), 6.5, PIX_AXIS, "middle")}'
               f'</g>')

    # worker
    for wk in workers:
        cnt = sum(1 for r in items if r["worker"] == wk)
        y = wy[wk]
        col = worker_color(wk)
        tint = {"DSH": "#F0EBFA", "Pi": "#E4F1F2", "Pi·rpc": "#FBF0DE",
                "Hermes": "#E6E6E2", "Hermes·sub": "#F7E4DE", "Codex": "#DFF0E9",
                "Claude": "#F6E3EF"}.get(wk, "#FFFCF3")
        out.append(f'<g class="gn" data-nid="w:{e(wk)}">'
                   f'<rect x="{wx}" y="{y:.1f}" width="{ww}" height="28" fill="{tint}" stroke="{col}" stroke-width="2"/>'
                   f'<rect x="{wx}" y="{y:.1f}" width="5" height="28" fill="{col}"/>'
                   f'{_t(wx + 12, y + 17.5, wk.upper(), 7.5, col)}'
                   f'{_t(wx + ww - 6, y + 17.5, f"x{cnt}", 7, PIX_AXIS, "end")}'
                   f'</g>')

    # task
    for i, r in enumerate(items):
        y = ty[i]
        st_col = _state_color(r["state"])
        label = _label_of(r)
        short = label if len(label) <= 26 else label[:25] + "…"
        tipx = f"{label} · {r['worker']} · {r['state']} · {human_tok(r.get('tokens'))} tok · {dur_text(r)} · {r['key']}"
        st_txt = state_label(r["state"])
        meta = f"{datetime.fromtimestamp(r['start'], TZ):%m/%d %H:%M} · {dur_text(r)} · {st_txt}"
        out.append(f'<g class="gn" data-nid="t:{e(r["key"])}">'
                   f'<rect x="{tx}" y="{y:.1f}" width="{tw}" height="28" fill="#FFFFFF" stroke="#141519" stroke-width="1.1">'
                   f'{_tip(tipx)}</rect>'
                   f'<rect x="{tx}" y="{y:.1f}" width="3.5" height="28" fill="{st_col}"/>'
                   f'<text x="{tx + 11}" y="{y + 13}" font-family="-apple-system,PingFang SC,sans-serif" font-size="10.5" fill="#23242A">{e(short)}</text>'
                   f'{_t(tx + 11, y + 23, meta, 6.2, "#6E7066")}'
                   f'{_t(tx + tw - 6, y + 13, human_tok(r.get("tokens")), 6, "#B0A894", "end")}'
                   f'</g>')

    # project
    for pr in projs:
        y = py[pr]
        cnt = sum(1 for r in items if (r.get("project") or "?") == pr)
        out.append(f'<g class="gn" data-nid="p:{e(pr)}">'
                   f'<rect x="{px}" y="{y:.1f}" width="{pw_}" height="28" fill="#EFEADA" stroke="#141519" stroke-width="1.1">'
                   f'{_tip(f"{pr} · {cnt} runs")}</rect>'
                   f'<text x="{px + 9}" y="{y + 18}" font-family="-apple-system,PingFang SC,sans-serif" font-size="10" fill="#33342E">{e(pr[:26])}</text>'
                   f'{_t(px + pw_ - 6, y + 18, f"x{cnt}", 6.5, PIX_AXIS, "end")}'
                   f'</g>')

    legend = (f'<span><i style="background:{PIX_DSH}"></i>DSH</span>'
              f'<span><i style="background:{PIX_PI}"></i>Pi</span>'
              f'<span><i style="background:{PIX_RPC}"></i>Pi·rpc</span>'
              f'<span><i style="background:{PIX_OK}"></i>{T("lg_done")}</span>'
              f'<span><i style="background:{PIX_RUN}"></i>{T("st_run")}</span>'
              f'<span><i style="background:{PIX_HALT}"></i>{T("lg_halt")}</span>'
              f'<span>{T("bus")} · {T("bus_hover")}</span>')
    svg = f'<svg class="net" viewBox="0 0 {w} {h}">{"".join(out)}</svg>'
    return _panel(T("p_graph"), T("h_graph", len(items)), svg, legend,
                  _ctl_grp("graph", [(6, "6"), (12, "12"), (24, "24")], n))

def network_block(recs, graph_n=12):
    return f'<div class="grid1">{chart_network(recs, n=graph_n)}</div>'


SLICE_PILL = {"passed": "p-ok", "rejected": "p-warn", "running": "p-run",
              "failed": "p-bad", "aborted": "p-bad", "planned": "p-ok"}
SLICE_STATE_KEY = {"planned": "sl_planned", "running": "sl_running",
                   "passed": "sl_passed", "rejected": "sl_rejected",
                   "failed": "sl_failed", "aborted": "sl_aborted"}
SLICE_VERDICT_KEY = {"pass": "vd_pass", "reject": "vd_reject",
                     "abort": "vd_abort", "na": "vd_na"}
# 账本里的 executor 是小写；复用主看板的 badge 配色。
SLICE_WORKER = {"dsh": "DSH", "pi": "Pi", "rpc": "Pi·rpc",
                "subagent": "Hermes·sub", "codex": "Codex", "claude": "Claude"}


def _slice_pill(state):
    return SLICE_PILL.get(state, "p-warn"), T(SLICE_STATE_KEY.get(state, "sl_failed"))


def _slice_verdict(v):
    if not v:
        return "—"
    return T(SLICE_VERDICT_KEY.get(v, "vd_na"))


def _slices_tag(total, verdict, pass_pct, avg):
    return T("slices_tag", total, verdict, pass_pct, avg) if verdict else T("slices_tag_none", total)


def _slices_empty_html(sdays):
    return f"""
<div class="panel">
  <div class="panel-h"><i class="sq" style="background:var(--amber);animation:none"></i>
    <h2 class="px">{T("p_slices")}</h2>
    <div class="right">{_ctl_grp("sdays", [(7, "7D"), (14, "14D"), (30, "30D")], sdays)}<span class="tag px">{_slices_tag(0, 0, 0, 0.0)}</span></div></div>
  <table class="slices">
    <colgroup><col style="width:84px"><col><col style="width:96px"><col style="width:56px"><col style="width:64px"><col style="width:56px"><col style="width:96px"></colgroup>
    <thead><tr><th>{T("th_state")}</th><th>{T("th_slice")}</th><th>{T("th_worker")}</th><th class="num">{T("th_attempt")}</th><th>{T("th_verdict")}</th><th class="num">{T("th_runs")}</th><th>{T("th_updated")}</th></tr></thead>
    <tbody><tr><td colspan="7" class="empty">{T("slices_empty")}</td></tr></tbody>
    <tfoot><tr><td colspan="7">{T("slices_show", 0, 0)}</td></tr></tfoot>
  </table>
</div>"""


def slices_panel(sdays=7):
    """SLICES 面板：只读 af_ledger；缺库/异常时降级为空状态，绝不拖垮整页。"""
    try:
        if L is None:
            return _slices_empty_html(sdays)
        data = L.read_slices(days=sdays, limit=50)
        stats = L.read_stats(days=sdays)
        slices = data.get("slices") or []
        groups, index = [], {}
        for sl in slices:
            rid = sl.get("req_id") or ""
            if rid not in index:
                index[rid] = []
                groups.append((rid, index[rid]))
            index[rid].append(sl)

        body = []
        for rid, items in groups:
            req_text = (items[0].get("req_text") or "").strip() or rid
            body.append(f'<tr class="req"><td colspan="7">{e(req_text[:60])}</td></tr>')
            for sl in sorted(items, key=lambda x: x.get("created") or 0):
                ex = (sl.get("executor") or "").lower()
                pcls, plabel = _slice_pill(sl.get("state"))
                bcls = "b-cron" if ex in ("cron", "inline") else worker_badge(SLICE_WORKER.get(ex, ex))
                updated = datetime.fromtimestamp(float(sl.get("updated") or 0), TZ)
                body.append(
                    f'<tr class="sl">'
                    f'<td><span class="pill {pcls}">{e(plabel)}</span></td>'
                    f'<td><span class="taskcell" title="{e(sl.get("goal"))}">{e(sl.get("goal"))}</span></td>'
                    f'<td><span class="badge {bcls}">{e(ex.upper())}</span></td>'
                    f'<td class="num">{int(sl.get("attempt") or 0)}</td>'
                    f'<td>{e(_slice_verdict(sl.get("last_verdict")))}</td>'
                    f'<td class="num">{int(sl.get("runs") or 0)}</td>'
                    f'<td class="t">{updated:%m-%d %H:%M}</td>'
                    f'</tr>')

        body_html = "\n".join(body) or f'<tr><td colspan="7" class="empty">{T("slices_empty")}</td></tr>'
        total = int(stats.get("slices_total") or 0)
        verdict = int(stats.get("verdict_slices") or 0)
        pass_pct = round(float(stats.get("pass_rate_first_try") or 0) * 100)
        avg = float(stats.get("avg_attempts") or 0)
        shown = len(slices)
        return f"""
<div class="panel">
  <div class="panel-h"><i class="sq" style="background:var(--amber);animation:none"></i>
    <h2 class="px">{T("p_slices")}</h2>
    <div class="right">{_ctl_grp("sdays", [(7, "7D"), (14, "14D"), (30, "30D")], sdays)}<span class="tag px">{_slices_tag(total, verdict, pass_pct, avg)}</span></div></div>
  <div class="tscroll"><table class="slices">
    <colgroup><col style="width:84px"><col><col style="width:96px"><col style="width:56px"><col style="width:64px"><col style="width:56px"><col style="width:96px"></colgroup>
    <thead><tr><th>{T("th_state")}</th><th>{T("th_slice")}</th><th>{T("th_worker")}</th><th class="num">{T("th_attempt")}</th><th>{T("th_verdict")}</th><th class="num">{T("th_runs")}</th><th>{T("th_updated")}</th></tr></thead>
    <tbody>{body_html}</tbody>
    <tfoot><tr><td colspan="7">{T("slices_show", shown, total)}</td></tr></tfoot>
  </table></div>
</div>"""
    except Exception:
        return _slices_empty_html(sdays)


def charts_html(recs, days=14, graph_n=12, procs=None, tdays=7, projs=6, scope="all", sdays=7):
    last = chart_durations(recs) if procs is None else processes_panel(procs, recs)
    grid = (chart_daily(recs, days=days) + chart_gantt(recs, days=tdays, scope=scope) +
            chart_projects(recs, top=projs) + last)
    return (f'<div class="grid2">{grid}</div>' + network_block(recs, graph_n) +
            slices_panel(sdays))



# ------------------------------------------------------------------ 界面文案（中/英）
I18N = {
    "zh": {
        "board_title": "执行者看板",
        "board_sub": "我派给 DSH / Pi 的任务，按真实会话事件流汇总",
        "k_running": "正在跑的任务", "k_runs": "今天派出的次数",
        "k_tok": "今天消耗（含缓存读）",
        "k_cost_pi": "DSH 估算 · Pi 另有 $%.3f", "k_cost_plain": "DSH 会话估算（Pi 未用）",
        "no_tools": "还没有工具调用（在读上下文/思考）",
        "records": "%d RECORDS", "empty_records": "还没有记录",
        "proc_title": "PROCESSES", "proc_live": "%d 个", "proc_none": "当前没有 agent 进程在跑",
        "th_pid": "PID", "th_agent": "执行者", "th_proj": "项目", "th_elapsed": "已运行",
        "th_cpu": "CPU", "th_task": "任务",
        "range": "范围", "rows": "行数", "graph": "拓扑", "lang": "语言",
        "cost_axis": "花费（右轴 ¥）", "dim_halt": "淡色=中断", "total": "合计",
        "price_note": "deepseek-flash 官价：缓存 ¥0.02 / 输入 ¥1 / 输出 ¥4 每百万 token，北京工作日 9-12、14-18 双倍",
        "cost_est": "（估算）", "cost_tokens": "缓存 %s / 输入 %s / 输出 %s tokens",
        "cost_upstream": "上游自报 $%.4f",
        "cost_today_tip": "今日 0:00 起 · 估算合计 ¥%.2f（%s）· Pi 组含 Pi/Pi·rpc/Codex，Hermes 组含主循环与子 agent · %s · 只是按 token 单价推算，不是账单实付",
        "active_tip": "活跃 %s（%d 段）", "lg_done": "完成", "lg_halt": "中断", "lg_fail": "失败",
        "dur_x": "x=开始时间（虚线=换日）", "bus_hover": "点/悬停节点只看它的连接",
        "elapsed": "已运行", "lang_zh": "中文", "lang_en": "EN",
        "k_cost_est": "官价估算·非实付", "sc_all": "全部", "sc_dg": "只看派活",
        "tl_lanes": "%d 条会话 · 最多 %d 道", "tl_segs": "%d 段", "p_now_runs": "%d 条在窗内",
        "bus": "竖线=同一项目汇聚 · 左→右=委派方向",
        "untitled": "（无标题）",
        "ctl_static": "静态快照 · 用 --serve 才能切换",
        "no_data": "无数据",
        "p_tokens": "TOKEN · %d天", "p_timeline": "时间线 · %d天", "h_timeline": "本地 0-24 时",
        "p_projects": "项目", "h_projects": "TOKEN·次数·占比",
        "p_duration": "耗时", "p_graph": "派活拓扑", "h_graph": "最近 %d 次",
        "p_proc": "进程", "p_now": "正在运行", "p_hist": "运行流水",
        "p_slices": "切片",
        "slices_tag": "%d 切片 · %d 已裁决 · 一次通过 %d%% · 平均 %.1f 次",
        "slices_tag_none": "%d 切片 · 暂无裁决（cron 同步不计入）",
        "slices_empty": "还没有切片记录 · 用 af_ledger.py 记一条",
        "slices_show": "显示 %d / 共 %d 条（窗口内）",
        "sl_planned": "计划中", "sl_running": "在跑", "sl_passed": "通过",
        "sl_rejected": "打回", "sl_failed": "失败", "sl_aborted": "中止",
        "th_slice": "切片", "th_attempt": "尝试", "th_verdict": "裁决",
        "th_runs": "RUNS", "th_updated": "更新",
        "vd_pass": "通过", "vd_reject": "打回", "vd_abort": "中止", "vd_na": "—",
        "th_time": "时间", "th_worker": "执行者", "th_project": "项目", "th_task": "任务",
        "th_state": "状态", "th_tokens": "TOKEN", "th_cost": "花费", "th_dur": "耗时",
        "dur_tip": "活跃时长：按事件流分段的实际活动时间，空闲超过 20 分钟断开（会话被隔天恢复时不会算成一整段）",
        "st_run": "在跑", "st_done": "完成", "st_halt": "中断", "st_abort": "中止", "st_fail": "失败",
        "foot_detail": "DETAIL: worker_runs.py show <KEY> · ¥ = deepseek-flash 官价估算（高峰 2×，非账单实付）",
        "kpi_run": "在跑", "kpi_runs": "今日派活", "kpi_tok": "TOKEN", "kpi_cost": "今日花费（估）",
        "kicker": "执行日志", "kicker2": "串行监控", "local_time": "本地时间", "sync": "同步",
        "lg_cache": "缓存", "lg_input": "输入", "lg_output": "输出",
        "max_label": "峰值 %s", "orch": "编排", "runs_tag": "%d 次",

    },
    "en": {
        "board_title": "Worker Board",
        "board_sub": "Every task handed to DSH / Pi, summed from the real session event stream",
        "k_running": "tasks running now", "k_runs": "dispatches today",
        "k_tok": "tokens today (cache included)",
        "k_cost_pi": "DSH estimate · Pi $%.3f", "k_cost_plain": "DSH session estimate (Pi unused)",
        "no_tools": "no tool calls yet (reading context / thinking)",
        "records": "%d RECORDS", "empty_records": "no records yet",
        "proc_title": "PROCESSES", "proc_live": "%d LIVE", "proc_none": "no agent process running",
        "th_pid": "PID", "th_agent": "AGENT", "th_proj": "PROJECT", "th_elapsed": "ELAPSED",
        "th_cpu": "CPU", "th_task": "TASK",
        "range": "RANGE", "rows": "ROWS", "graph": "GRAPH", "lang": "LANG",
        "cost_axis": "cost (right axis ¥)", "dim_halt": "dim = interrupted", "total": "total",
        "price_note": "deepseek-flash list price: cache ¥0.02 / input ¥1 / output ¥4 per M tokens, doubled on Beijing weekdays 9-12 & 14-18",
        "cost_est": "(est.)", "cost_tokens": "cache %s / input %s / output %s tokens",
        "cost_upstream": "upstream $%.4f",
        "cost_today_tip": "since 00:00 · est. total ¥%.2f (%s) · Pi group = Pi/Pi·rpc/Codex, Hermes group = main loop + sub-agents · %s · derived from token prices, not an invoice",
        "active_tip": "active %s (%d seg)", "lg_done": "DONE", "lg_halt": "HALT", "lg_fail": "FAIL",
        "dur_x": "x = start time (dashed = new day)", "bus_hover": "tap/hover a node to keep only its links",
        "elapsed": "ELAPSED", "lang_zh": "ZH", "lang_en": "EN",
        "k_cost_est": "est. · not billed", "sc_all": "ALL", "sc_dg": "DELEGATED",
        "tl_lanes": "%d sessions · up to %d lanes", "tl_segs": "%d seg", "p_now_runs": "%d in view",
        "bus": "bar = project bus · left→right = dispatch",
        "untitled": "(untitled)",
        "ctl_static": "static snapshot · run with --serve to switch",
        "no_data": "no data",
        "p_tokens": "TOKENS / %dD", "p_timeline": "TIMELINE / %dD", "h_timeline": "LOCAL 0-24H",
        "p_projects": "PROJECTS", "h_projects": "TOKENS·RUNS·SHARE",
        "p_duration": "DURATION", "p_graph": "DELEGATION GRAPH", "h_graph": "RECENT %d",
        "p_proc": "PROCESSES", "p_now": "NOW RUNNING", "p_hist": "RUN HISTORY",
        "p_slices": "SLICES",
        "slices_tag": "%d slices · %d verdicts · %d%% first-try pass · avg %.1f attempts",
        "slices_tag_none": "%d slices · no verdicts yet (cron sync excluded)",
        "slices_empty": "no slices yet · record one with af_ledger.py",
        "slices_show": "showing %d / %d (in window)",
        "sl_planned": "PLANNED", "sl_running": "RUNNING", "sl_passed": "PASSED",
        "sl_rejected": "REJECTED", "sl_failed": "FAILED", "sl_aborted": "ABORTED",
        "th_slice": "SLICE", "th_attempt": "ATTEMPT", "th_verdict": "VERDICT",
        "th_runs": "RUNS", "th_updated": "UPDATED",
        "vd_pass": "PASS", "vd_reject": "REJECT", "vd_abort": "ABORT", "vd_na": "NA",
        "th_time": "TIME", "th_worker": "WORKER", "th_project": "PROJECT", "th_task": "TASK",
        "th_state": "STATE", "th_tokens": "TOKENS", "th_cost": "COST", "th_dur": "DUR",
        "dur_tip": "active time: event-stream segments, split when idle > 20 min (a session resumed next day is not one long run)",
        "st_run": "RUNNING", "st_done": "DONE", "st_halt": "HALT", "st_abort": "ABORT", "st_fail": "FAIL",
        "foot_detail": "DETAIL: worker_runs.py show <KEY> · ¥ = EST. @ DEEPSEEK-FLASH LIST PRICE (PEAK 2×, NOT BILLED)",
        "kpi_run": "RUNNING", "kpi_runs": "TODAY RUNS", "kpi_tok": "TOKENS", "kpi_cost": "COST TODAY (EST.)",
        "kicker": "EXECUTION LOG", "kicker2": "SERIAL MONITOR", "local_time": "LOCAL TIME", "sync": "SYNC",
        "lg_cache": "cache", "lg_input": "input", "lg_output": "output",
        "max_label": "MAX %s", "orch": "ORCHESTRATOR", "runs_tag": "%d RUNS",

    },
}
LANG = {"v": "zh"}


def T(key, *args):
    s = I18N.get(LANG["v"], I18N["zh"]).get(key, key)
    return s % args if args else s


CTL_CSS = """
.lang-sw{margin-top:9px;display:flex;align-items:center;justify-content:flex-end}
.lang-sw .pctl a{padding:4px 8px;font-size:8.5px}
.panel-h .right{display:flex;align-items:center;gap:8px}
.pctl{display:inline-flex;gap:3px;align-items:center}
.pctl a{border:1.5px solid #141519;background:#FBF8EE;color:#141519;padding:2px 5px;
        text-decoration:none;font-family:'PS2P',monospace;font-size:7px;box-shadow:1.5px 1.5px 0 #141519}
.pctl a.on{background:#141519;color:#FBF8EE}
.pctl a.busy{opacity:.45;transform:translate(1px,1px);box-shadow:none}
.pctl a.busy:after{content:"…"}
.pctl a:hover{transform:translate(-1px,-1px);box-shadow:2.5px 2.5px 0 #141519}
.ctl{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:10px 0 4px;
     font-family:'PS2P',monospace;font-size:8px;color:var(--ink-3)}
.ctl .grp{display:flex;gap:6px;align-items:center}
.ctl .grp>b{font-weight:400;opacity:.6;margin-right:2px}
.ctl a{border:2px solid #141519;background:#FBF8EE;color:#141519;padding:4px 7px;
       text-decoration:none;box-shadow:2px 2px 0 #141519;font-size:8px}
.ctl a.on{background:#141519;color:#FBF8EE}
.ctl a:hover{transform:translate(-1px,-1px);box-shadow:3px 3px 0 #141519}
"""

NET_CSS = """
svg.net .eg,svg.net .gn{transition:opacity .12s}
svg.net .gn{cursor:pointer}
svg.net.hasfocus .eg.dim,svg.net.hasfocus .gn.dim{opacity:.12}
svg.net.hasfocus .eg.hl path{stroke-width:2.8}
svg.net.hasfocus .gn.hl rect{stroke-width:3}
"""

NET_JS = """
<script>
window.__netInit = function(){
  var svg = document.querySelector('svg.net');
  if(!svg) return;
  var edges = [].slice.call(svg.querySelectorAll('.eg'));
  var nodes = [].slice.call(svg.querySelectorAll('.gn'));
  var hovered = null, pinned = null;
  var meta = document.querySelector('meta[http-equiv=refresh]');
  var spec = meta ? meta.getAttribute('content') : null;
  function pause(){ if(meta){ meta.parentNode.removeChild(meta); meta = null; } }
  function resume(){
    if(spec && !document.querySelector('meta[http-equiv=refresh]')){
      var m = document.createElement('meta');
      m.setAttribute('http-equiv','refresh'); m.setAttribute('content', spec);
      document.head.appendChild(m); meta = m;
    }
  }
  function related(id){
    return edges.filter(function(g){
      var a = g.getAttribute('data-a'), b = g.getAttribute('data-b'), pr = g.getAttribute('data-proj');
      if(a === id || b === id) return true;
      if(id.indexOf('p:') === 0 && pr === id.slice(2)) return true;          // project: all dispatches under it
      if(id.indexOf('t:') === 0 && pr && a === 'bus:' + pr) return true;     // task: the bus into its project
      return false;
    });
  }
  function apply(){
    var id = hovered || pinned;
    svg.classList.toggle('hasfocus', !!id);
    if(!id){
      edges.forEach(function(g){ g.classList.remove('hl','dim'); });
      nodes.forEach(function(g){ g.classList.remove('hl','dim'); });
      return;
    }
    var rel = related(id), keep = {};
    keep[id] = 1;
    rel.forEach(function(g){ keep[g.getAttribute('data-a')] = 1; keep[g.getAttribute('data-b')] = 1; });
    edges.forEach(function(g){ var on = rel.indexOf(g) >= 0; g.classList.toggle('hl', on); g.classList.toggle('dim', !on); });
    nodes.forEach(function(n){
      var k = n.getAttribute('data-nid'), on = !!keep[k];
      n.classList.toggle('hl', k === id);
      n.classList.toggle('dim', !on);
    });
  }
  svg.addEventListener('mouseover', function(ev){
    var g = ev.target.closest && ev.target.closest('.gn');
    var id = g ? g.getAttribute('data-nid') : null;
    if(id !== hovered){ hovered = id; apply(); if(id) pause(); }
  });
  svg.addEventListener('mouseleave', function(){ hovered = null; apply(); if(!pinned) resume(); });
  svg.addEventListener('click', function(ev){
    var g = ev.target.closest && ev.target.closest('.gn');
    var id = g ? g.getAttribute('data-nid') : null;
    pinned = (id && pinned !== id) ? id : null;
    apply();
    if(pinned) pause(); else resume();
  });
  window.__netReset = function(){ pinned = null; hovered = null; apply(); resume(); };
  var q = (location.search.match(/[?&]focus=([^&]+)/) || [])[1];
  if(q){ pinned = decodeURIComponent(q); apply(); pause(); }
};
if(!window.__netKey){
  window.__netKey = function(ev){ if(ev.key === 'Escape' && window.__netReset) window.__netReset(); };
  document.addEventListener('keydown', window.__netKey);
}
window.__netInit();
</script>
"""

# 范围/行数切换：预取 + 原地换 DOM，不再整页重新导航（保留滚动位置，点下去立刻有反馈）
SWAP_JS = """
<script>
(function(){
  if(location.protocol === 'file:') return;
  var cache = {}, order = [], inflight = {}, token = 0;
  function ctlOf(el){ return (el && el.closest) ? el.closest('.pctl a') : null; }
  function remember(url, html){
    if(!html || cache[url]) return;
    cache[url] = html; order.push(url);
    if(order.length > 24) delete cache[order.shift()];
  }
  function prefetch(url){
    if(!url || cache[url] || inflight[url]) return;
    inflight[url] = 1;
    fetch(url, {credentials:'same-origin'})
      .then(function(r){ return r.ok ? r.text() : ''; })
      .then(function(t){ remember(url, t); })
      .catch(function(){})
      .then(function(){ delete inflight[url]; });
  }
  function applyDoc(url, html){
    var doc = new DOMParser().parseFromString(html, 'text/html');
    if(!doc.body){ location.href = url; return; }
    var y = window.scrollY;
    var om = document.querySelector('meta[http-equiv=refresh]');
    var nm = doc.querySelector('meta[http-equiv=refresh]');
    if(om) om.parentNode.removeChild(om);                 // re-arm the refresh timer; don't reload right after a switch
    if(nm){
      var m = document.createElement('meta');
      m.setAttribute('http-equiv','refresh'); m.setAttribute('content', nm.getAttribute('content'));
      document.head.appendChild(m);
    }
    document.title = doc.title || document.title;
    if(doc.documentElement && doc.documentElement.lang) document.documentElement.lang = doc.documentElement.lang;
    document.body.innerHTML = doc.body.innerHTML;
    window.scrollTo(0, y);
    try{ history.pushState(null, '', url); }catch(e){}
    if(window.__netInit) window.__netInit();
    if(window.__liveSeed) window.__liveSeed();
  }
  document.addEventListener('mouseover', function(ev){
    var a = ctlOf(ev.target);
    if(a) prefetch(a.getAttribute('href'));
  });
  document.addEventListener('click', function(ev){
    var a = ctlOf(ev.target);
    if(!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || (ev.button || 0) > 0) return;
    var url = a.getAttribute('href');
    if(!url || url.charAt(0) === '#' || a.classList.contains('on')) return;
    ev.preventDefault();
    var old = document.querySelector('.pctl a.busy');
    if(old) old.classList.remove('busy');
    a.classList.add('busy');
    var my = ++token;
    function done(html){
      if(my !== token) return;                            // only honour the last click
      a.classList.remove('busy');
      remember(url, html);
      applyDoc(url, html);
    }
    if(cache[url]){ done(cache[url]); return; }
    fetch(url, {credentials:'same-origin'})
      .then(function(r){ if(!r.ok) throw new Error('http'); return r.text(); })
      .then(done)
      .catch(function(){ location.href = url; });
  });
  window.addEventListener('popstate', function(){ location.reload(); });
})();
</script>
"""

# 手机端：meta refresh 每 N 秒整页重载会把页面弹回顶部；按会话记住滚动位置，重载后再复位
MOBILE_JS = """
<script>
(function(){
  if(location.protocol === 'file:') return;
  var KEY = 'wd_scroll_y', y = 0;
  try{ y = parseInt(sessionStorage.getItem(KEY) || '0', 10) || 0; }catch(e){}
  function restore(){ if(y > 4) window.scrollTo(0, y); }
  if(document.readyState === 'complete'){ restore(); }
  else { window.addEventListener('load', function(){ setTimeout(restore, 0); }); }
  var t = null;
  window.addEventListener('scroll', function(){
    clearTimeout(t);
    t = setTimeout(function(){
      try{ sessionStorage.setItem(KEY, String(Math.round(window.scrollY))); }catch(e){}
    }, 200);
  }, {passive:true});
})();
</script>
"""

# 增量刷新：不再整页重载（meta refresh 会把你拉回顶部），改成
#   1) 每 N 秒问一次 ?frag=manifest（只回各块的哈希，很小）；
#   2) 只有哈希变了的块才去取 HTML 并原地替换；
#   3) 时钟与「在跑」耗时在本地每秒走，不靠刷新。
# 交互中（滚动/触摸/锁定了拓扑节点）暂停轮询，避免打断阅读。
LIVE_JS = """
<script>
(function(){
  if(location.protocol === 'file:') return;
  var POLL = (parseInt((document.body && document.body.getAttribute('data-poll')) || '0', 10) || 0) * 1000;
  var cur = {}, ready = false, busyUntil = 0, hints = {}, lastSwap = {};
  function params(){
    return location.search.replace(/^\\?/, '').split('&').filter(function(s){
      return s && s.indexOf('frag=') !== 0;
    }).join('&');
  }
  function jget(u){
    return fetch(u, {cache:'no-store'}).then(function(r){ return r.ok ? r.json() : null; });
  }
  function tget(u){
    return fetch(u, {cache:'no-store'}).then(function(r){ return r.ok ? r.text() : ''; });
  }
  function marks(){
    var m = {};
    (function scan(node){
      for(var n = node.firstChild; n; n = n.nextSibling){
        if(n.nodeType === 8){
          var t = (n.data || '').trim();
          if(t.indexOf('F:/') === 0){ if(m[t.slice(3)]) m[t.slice(3)].end = n; }
          else if(t.indexOf('F:') === 0){ m[t.slice(2)] = {start:n}; }
        } else if(n.nodeType === 1 && n.tagName !== 'SCRIPT'){
          scan(n);
        }
      }
    })(document.body);
    return m;
  }
  function swap(name, html){
    if(!html) return;
    var m = marks()[name];
    if(!m || !m.start || !m.end) return;
    var r = document.createRange();
    r.setStartAfter(m.start);
    r.setEndBefore(m.end);
    r.deleteContents();
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var f = document.createDocumentFragment();
    while(tmp.firstChild) f.appendChild(tmp.firstChild);
    r.insertNode(f);
    if(name === 'network' && window.__netInit) window.__netInit();
    if(name === 'charts' || name === 'network') tick();
  }
  function seed(m){
    cur = {};
    if(m && m.frags){ for(var k in m.frags) cur[k] = m.frags[k]; }
    if(m && m.hints){ hints = m.hints; }
    ready = true;
  }
  function poll(){
    if(!POLL || document.hidden) return;
    if(Date.now() < busyUntil) return;                                  // 正在读/滑，不刷
    if(document.querySelector('svg.net.hasfocus')) return;              // 拓扑被锁定
    jget('?frag=manifest&' + params()).then(function(m){
      if(!m || !m.frags) return;
      if(!ready){ seed(m); return; }
      if(m.hints) hints = m.hints;
      for(var n in m.frags){
        if(cur[n] === m.frags[n]) continue;
        var gap = (hints[n] || (POLL / 1000)) * 1000;
        if(lastSwap[n] && Date.now() - lastSwap[n] < gap) continue;      // 该块还没到刷新间隔
        cur[n] = m.frags[n];
        lastSwap[n] = Date.now();
        (function(nm){
          tget('?frag=' + nm + '&' + params()).then(function(h){ swap(nm, h); });
        })(n);
      }
    }).catch(function(){});
  }
  function two(n){ return ('0' + n).slice(-2); }
  function fmt(sec){
    sec = Math.max(0, Math.floor(sec));
    if(sec < 3600) return Math.floor(sec / 60) + ':' + two(sec % 60);
    return Math.floor(sec / 3600) + ':' + two(Math.floor((sec % 3600) / 60)) + ':' + two(sec % 60);
  }
  function tick(){
    var clk = document.getElementById('clk');
    if(clk){
      var d = new Date();
      clk.textContent = two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
    }
    var now = Math.floor(Date.now() / 1000), els = document.querySelectorAll('[data-since]');
    for(var i = 0; i < els.length; i++){
      var s = parseInt(els[i].getAttribute('data-since'), 10);
      if(!isNaN(s)) els[i].textContent = fmt(now - s);
    }
  }
  window.__liveSeed = function(){ jget('?frag=manifest&' + params()).then(seed).catch(function(){}); };
  window.__livePoll = poll;              // 便于自测/手动催一次
  function touch(){ busyUntil = Date.now() + 6000; }
  ['touchstart','pointerdown','wheel','scroll','keydown'].forEach(function(ev){
    window.addEventListener(ev, touch, {passive:true});
  });
  setInterval(tick, 1000); tick();
  if(POLL){
    jget('?frag=manifest&' + params()).then(function(m){ seed(m); }).catch(function(){});
    setInterval(poll, POLL);
  }
})();
</script>
"""



CUR = {}


def _qs(**kw):
    d = dict(CUR)
    d.update({k: str(v) for k, v in kw.items()})
    return "?" + urlencode(d)


def _ctl(key, label, value, current):
    cls = ' class="on"' if str(current) == str(value) else ""
    return f'<a{cls} href="{_qs(**{key: value})}">{label}</a>'


def _ctl_grp(key, pairs, current):
    return ('<span class="pctl">' +
            ''.join(_ctl(key, lb, v, current) for v, lb in pairs) + '</span>')


def processes_panel(procs, recs):
    """PROCESSES 只列真实 OS 进程。

    PID 只能来自 ps / pi-rpc 的 meta.json —— 绝不能拿会话冒充进程：
    旧版给每条 RUNNING 会话挂同一个 hermes_pid()，于是两行同一个 PID（还是别的
    profile 的网关），同时把真正在跑的常驻 pi 进程漏掉。
    """
    rows = []
    by_pid = {r.get("pid"): r for r in recs if r.get("pid")}
    have = {pr.get("pid") for pr in procs}
    for r in recs:
        if r.get("state") != "RUNNING":
            continue
        pid = r.get("pid")
        if not pid or pid in have:
            continue
        procs = procs + [{"pid": pid, "agent": r.get("worker"), "cwd": "",
                          "project": r.get("project") or "?", "elapsed": dur_lcd(r),
                          "cpu": "—", "task": clean_title(r.get("label"))}]
        have.add(pid)
    for pr in procs[:8]:
        rec = by_pid.get(pr.get("pid"))
        # 进程自己的任务文本（ps/meta 给的）优先，其次才用会话标题
        task = clean_title(pr.get("task")) or (clean_title(rec.get("label")) if rec else "") or "—"
        badge = {"DSH": "b-dsh", "Pi": "b-pi", "Pi·rpc": "b-rpc", "Codex": "b-rpc",
                 "Claude": "b-claude"}.get(pr["agent"], "b-pi")
        rows.append(
            f'<tr><td class="num t">{pr["pid"]}</td>'
            f'<td><span class="badge {badge}">{e(pr["agent"].upper())}</span></td>'
            f'<td><span class="proj" title="{e(pr["cwd"])}">{e(pr["project"])}</span></td>'
            f'<td class="num t">{e(pr["elapsed"])}</td>'
            f'<td class="num t">{e(pr["cpu"])}</td>'
            f'<td><span class="taskcell" title="{e(task)}">{e(task[:60])}</span></td></tr>')
    body = "".join(rows) or f'<tr><td colspan="6" class="empty">{T("proc_none")}</td></tr>'
    return f"""
<div class="panel">
  <div class="panel-h"><i class="sq" style="background:var(--violet);animation:none"></i><h2 class="px">{T("p_proc")}</h2>
    <div class="right"><span class="tag px">{T("proc_live", len(procs))}</span></div></div>
  <div class="tscroll"><table class="proc">
    <colgroup><col style="width:76px"><col style="width:74px"><col style="width:132px"><col style="width:74px"><col style="width:56px"><col></colgroup>
    <thead><tr><th>{T("th_pid")}</th><th>{T("th_agent")}</th><th>{T("th_proj")}</th><th>{T("th_elapsed")}</th><th>{T("th_cpu")}</th><th>{T("th_task")}</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</div>"""


def render_pixel(recs, refresh, lang="zh", days=14, rows=20, graph_n=12, procs=None, cur=None,
                 tdays=7, projs=6, scope="all", sdays=7):
    LANG["v"] = "en" if lang == "en" else "zh"
    cur = cur or {"lang": lang, "days": days, "rows": rows, "graph": graph_n,
                  "tdays": tdays, "projs": projs, "scope": scope, "sdays": sdays}
    cur = dict(cur)
    cur.setdefault("sdays", sdays)
    CUR.clear(); CUR.update(cur)
    sdays = int(cur.get("sdays", sdays))
    procs = procs or []
    row_limit = int(rows)
    now = datetime.now(TZ)
    today = now.date()
    running = [r for r in recs if r["state"] == "RUNNING"]
    today_recs = [r for r in recs if datetime.fromtimestamp(r["start"], TZ).date() == today]
    tok_today = sum(r.get("tokens") or 0 for r in today_recs)
    grp_today = {"DSH": 0.0, "Hermes": 0.0, "Pi": 0.0}
    for r in today_recs:
        w = r.get("worker") or ""
        g = "DSH" if w == "DSH" else ("Hermes" if w.startswith("Hermes") else "Pi")
        grp_today[g] += rec_cny(r)
    cny_today = sum(grp_today.values())
    grp_today = {k: round(v, 2) for k, v in grp_today.items()}
    cny_today = round(sum(grp_today.values()), 2)   # 先各自四舍五入再加：明细相加必须等于合计
    cost_break = " · ".join(f"{k} ¥{v:.2f}" for k, v in grp_today.items() if v)
    cost_today_tip = T("cost_today_tip", cny_today, cost_break, T("price_note"))
    maxtok = max([r.get("tokens") or 0 for r in recs] + [1])

    head = f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{T("board_title")} · PIXEL</title>
<style>{_font_css()}{CSS_PIXEL}{CTL_CSS}{NET_CSS}</style></head><body data-poll="{int(refresh or 0)}"><div class="wrap">"""

    top = f"""
<div class="top">
  <div>
    <div class="kicker px"><b>{T("kicker")}</b> DSH / PI · {T("kicker2")}</div>
    <h1>{T("board_title")}<span class="rule"></span></h1>
    <div class="sub">{T("board_sub")}</div>
  </div>
  <div class="clock">
    <div class="lbl px">{T("local_time")}</div>
    <div class="lcd"><div class="t px" id="clk">{now:%H:%M:%S}</div></div>
    <div class="d px"><span>{now:%m-%d} {weekday_label(now.weekday())}</span><span>{T("sync")} <i class="blink"></i></span></div>
    <div class="lang-sw"><span class="lbl px" style="margin-right:6px">{T("lang")}</span>{_ctl_grp("lang", [("zh", T("lang_zh")), ("en", T("lang_en"))], lang)}</div>
  </div>
</div>

<div class="stats">
  <div class="stat{' hot' if running else ''}">
    <div class="row1"><span class="k px">{T("kpi_run")}</span>{sprite('run')}</div>
    <div class="lcd"><div class="v px">{len(running):02d}</div></div><div class="c">{T("k_running")}</div>
  </div>
  <div class="stat">
    <div class="row1"><span class="k px">{T("kpi_runs")}</span>{sprite('stack')}</div>
    <div class="lcd"><div class="v px">{len(today_recs):02d}</div></div><div class="c">{T("k_runs")}</div>
  </div>
  <div class="stat">
    <div class="row1"><span class="k px">{T("kpi_tok")}</span>{sprite('chip')}</div>
    <div class="lcd"><div class="v px">{human_tok(tok_today).upper()}</div></div><div class="c">{T("k_tok")}</div>
  </div>
  <div class="stat" title="{e(cost_today_tip)}">
    <div class="row1"><span class="k px">{T("kpi_cost")}</span>{sprite('coin')}</div>
    <div class="lcd"><div class="v px">≈¥{cny_today:.2f}</div></div>
    <div class="c2"><span>DSH <b>¥{grp_today['DSH']:.2f}</b></span><span>Hermes <b>¥{grp_today['Hermes']:.2f}</b></span><span>Pi/Codex/Claude <b>¥{grp_today['Pi']:.2f}</b></span></div>
    <div class="c2 dim">{T("k_cost_est")}</div>
  </div>
</div>"""

    last = chart_durations(recs) if procs is None else processes_panel(procs, recs)
    charts = ('<div class="grid2">' + chart_daily(recs, days=days) +
              chart_gantt(recs, days=tdays, scope=scope) +
              chart_projects(recs, top=projs) + last + '</div>')
    network = network_block(recs, graph_n)
    slices = slices_panel(sdays)

    live = ""
    if running:
        # 按最后活动时间排，不是按“谁先开始”：多个执行者同时在跑时，每一条都要能看到
        running = sorted(running, key=lambda r: r.get("last") or 0, reverse=True)
        cards = []
        for idx, r in enumerate(running[:4]):
            calls = (r.get("tool_calls") or [])[-7:]
            tl = "".join(
                f"<li><time>{datetime.fromtimestamp(ts, TZ):%H:%M:%S}</time>"
                f"<span class='tool'>{e(name.upper())}</span><span class='arg' title='{e(arg[:400])}'>{e(arg[:160])}</span></li>"
                for ts, name, arg in calls
            ) or f"<li><span class='arg'>{T('no_tools')}</span></li>"
            nth = (f' <span class="tag px">{idx + 1}/{len(running)}</span>'
                   if len(running) > 1 else "")
            cards.append(f"""
<div class="panel">
  <div class="panel-h"><i class="sq"></i><h2 class="px">{T("p_now")}</h2>{nth}
    <div class="right"><span class='tag live px' title='{T("elapsed")} {dur_text(r)}'>ELAPSED <span data-since="{int(r.get('start') or 0)}">{dur_lcd(r)}</span></span>
    <span class="tag px">{human_tok(r.get('tokens')).upper()} TOK</span>
    <span class="tag px" title="{e(cost_tip(r))}">{cost_text(r)}</span></div></div>
  <div class="live">
    <p class="task">{e(clean_title(r.get('label')))}</p>
    <div class="meta"><span>{e(r.get('project'))}</span><b>·</b><span class="led"><i></i>{T("st_run")}</span><b>·</b>
      <span class="id">{e(r.get('worker', ''))} {e(r['key'])}</span></div>
    <ul class="timeline">{tl}</ul>
  </div>
</div>""")
        live = "".join(cards)

    rows = []
    for r in recs[:row_limit]:
        cls = PIXEL_STATE.get(r["state"], "p-ok")
        label = state_label(r["state"])
        w = r.get("worker", "")
        pct = int(100 * (r.get("tokens") or 0) / maxtok)
        rows.append(f"""<tr>
  <td class="t">{datetime.fromtimestamp(r['start'], TZ):%m-%d %H:%M}</td>
  <td><span class="badge {worker_badge(w)}">{e(w.upper())}</span></td>
  <td><span class="proj" title="{e(r.get('project'))}">{e(r.get('project'))}</span></td>
  <td><span class="taskcell" title="{e(clean_title(r.get('label')))} · {e(r['key'])}">{e(clean_title(r.get('label')))}</span></td>
  <td><span class="pill {cls}">{e(label)}</span></td>
  <td class="num"><div class="tokcell"><span class="toknum">{human_tok(r.get('tokens')).upper()}</span>
      <span class="seg">{seg_bar(pct)}</span></div></td>
  <td class="num money" title="{e(cost_tip(r))}">{cost_text(r)}</td>
  <td class="num t">{f'<span data-since="{int(r.get("start") or 0)}">{dur_lcd(r)}</span>' if r["state"] == "RUNNING" else dur_lcd(r)}</td>
</tr>""")

    table = f"""
<div class="panel">
  <div class="panel-h"><i class="sq" style="background:var(--violet);animation:none"></i><h2 class="px">{T("p_hist")}</h2>
    <div class="right">{_ctl_grp("rows", [(20, "20"), (50, "50"), (100, "100")], row_limit)}<span class="tag px">{T("records", len(recs))}</span></div></div>
  <div class="tscroll"><table class="hist">
    <colgroup><col style="width:104px"><col style="width:76px"><col style="width:146px"><col><col style="width:92px"><col style="width:150px"><col style="width:84px"><col style="width:82px"></colgroup>
    <thead><tr><th>{T("th_time")}</th><th>{T("th_worker")}</th><th>{T("th_project")}</th><th>{T("th_task")}</th><th>{T("th_state")}</th><th>{T("th_tokens")}</th><th>{T("th_cost")}</th><th title="{T('dur_tip')}">{T("th_dur")}</th></tr></thead>
    <tbody>{''.join(rows) if rows else f'<tr><td colspan="8" class="empty">{T("empty_records")}</td></tr>'}</tbody>
  </table></div>
</div>"""

    tail = f"""
<div class="foot">
  <span class="px">SRC: ~/.dsh/sessions · ~/.pi/agent/sessions · ~/.hermes/logs/pi-rpc-*.jsonl</span>
  <span class="px">{T("foot_detail")}</span>
</div>
</div>{NET_JS}{SWAP_JS}{MOBILE_JS}{LIVE_JS}</body></html>"""
    # 六个可独立替换的块：顶部 KPI / 图表 / 拓扑 / 切片 / 正在运行 / 运行流水。
    # 浏览器只替换哈希变化的块 —— 不整页重载，滚动位置不动。
    return (head +
            "<!--F:top-->" + top + "<!--F:/top-->" +
            "<!--F:charts-->" + charts + "<!--F:/charts-->" +
            "<!--F:network-->" + network + "<!--F:/network-->" +
            "<!--F:slices-->" + slices + "<!--F:/slices-->" +
            "<!--F:live-->" + live + "<!--F:/live-->" +
            "<!--F:table-->" + table + "<!--F:/table-->" + tail)


def demo_recs(lang="zh"):
    """演示数据：虚构项目/任务，固定种子，7 天 26 条（供截图、陌生人试用）。"""
    import random
    rnd = random.Random(20261017)
    now = time.time()
    day0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    projs = ["my-app", "api-service", "docs-site"]
    titles = {
        "zh": {
            "my-app": ["导出器合并", "补齐边界用例", "慢路径 profile", "结构化日志改造"],
            "api-service": ["新增批量接口", "限流中间件", "数据迁移脚本", "排查 502"],
            "docs-site": ["README 英文化", "快速开始截图", "导航结构重排"],
        },
        "en": {
            "my-app": ["merge exporter paths", "add edge-case tests", "profile the slow path", "structured logging"],
            "api-service": ["add batch endpoint", "rate-limit middleware", "migration script", "debug the 502s"],
            "docs-site": ["translate the README", "quickstart screenshots", "rework the nav tree"],
        },
    }[lang]
    workers = [("DSH", "dsh"), ("DSH", "dsh"), ("DSH", "dsh"), ("Pi·rpc", "rpc"), ("Pi", "pi")]
    out = []
    seq = 0
    for d in range(6, -1, -1):
        day = day0 - d * 86400
        n = 2 if d == 0 else rnd.randint(3, 5)
        for i in range(n):
            worker, kind = rnd.choice(workers)
            proj = rnd.choice(projs)
            title = rnd.choice(titles[proj])
            start = day + (9 + (i * 2) % 9) * 3600 + rnd.randint(0, 1800)
            dur = rnd.choice([120, 300, 480, 900, 1500, 2400])
            if d == 0 and i >= n - 2:
                start = now - (n - i) * 600
                state = "RUNNING" if i == n - 1 else "interrupted"
                end = 0.0 if state == "RUNNING" else start + 300
            else:
                state, end = "done", start + dur
            tin = rnd.randint(8, 140) * 1000
            tcache = rnd.randint(20, 900) * 10000
            tout = rnd.randint(3, 40) * 1000
            tok = tin + tcache + tout
            rec = {
                "worker": worker, "kind": kind, "key": f"demo{seq}",
                "state": state, "tokens": tok, "tok_in": tin, "tok_out": tout, "tok_cache": tcache,
                "title": title, "task": title, "project": proj, "label": title,
                "start": start, "end": end, "turns": rnd.randint(4, 40),
                "steps": 0, "tools": {}, "tool_calls": [], "last_text": "",
                "created": start, "last": start + 60,
            }
            if state == "RUNNING":
                rec["tool_calls"] = [
                    (start + 60, "read", f"{proj}/src/main.py"),
                    (start + 180, "edit", f"{proj}/src/{'exporter' if proj == 'my-app' else 'handlers'}.py"),
                    (start + 300, "bash", "cargo test --release -q"),
                ]
            span = (end or time.time()) - start
            rec["cost_cny"] = W.cost_cny_of(tin, tout, tcache, start)
            rec["cost_usd"] = round(rec["cost_cny"] / FX, 5)
            rec["segs"] = ([[start, start + span * 0.45], [start + span * 0.72, end or time.time()]]
                           if span > 1200 else [[start, end or time.time()]])
            out.append(rec)
            seq += 1
    # 补几条 Hermes / 子 agent / Codex，让演示里六种执行者都出现
    extra = [
        ("Hermes", "hermes", "desktop session · 修 dashboard", "hermes·desktop", 3),
        ("Hermes", "hermes", "cron · 汇率播报", "hermes·cron", 40),
        ("Hermes·sub", "subagent", "把 README 翻成英文", "hermes·sub", 55),
        ("Hermes·sub", "subagent", "排查 CI 里的 flaky 测试", "hermes·sub", 90),
        ("Codex", "codex", "review the patch", "my-app", 120),
    ]
    for idx, (worker, kind, label, proj, back) in enumerate(extra):
        st = now - back * 60
        dur = [1500, 90, 300, 600, 420][idx]
        tin, tout, tc = 40000 + idx * 9000, 8000 + idx * 500, 300000 + idx * 50000
        out.append({
            "worker": worker, "kind": kind, "key": f"demo-{kind}-{idx}", "path": "",
            "state": "done", "tokens": tin + tout + tc, "tok_in": tin, "tok_out": tout, "tok_cache": tc,
            "cost_usd": round(tin * 0.14e-6 + tout * 4e-6 + tc * 0.014e-6, 5),
            "cost_cny": W.cost_cny_of(tin, tout, tc, st),
            "segs": [[st, st + dur]],
            "title": label, "task": label, "label": label, "project": proj,
            "start": st, "end": st + dur, "turns": 6, "steps": 0, "tools": {}, "tool_calls": [],
            "last_text": "", "created": st, "last": st + dur, "last_start": st + dur,
        })
    out.sort(key=lambda r: r["start"], reverse=True)
    return out


def demo_procs(lang="zh"):
    t = ("重构导出流水线", "") if lang == "zh" else ("rework the exporter pipeline", "")
    return [
        {"pid": 48213, "agent": "DSH", "cwd": "/Users/you/code/my-app", "project": "my-app",
         "elapsed": "07:42", "cpu": "3.1", "task": t[0]},
        {"pid": 48390, "agent": "Pi·rpc", "cwd": "/Users/you/code/api-service", "project": "api-service",
         "elapsed": "1:12:08", "cpu": "0.4", "task": ""},
        {"pid": 48402, "agent": "Hermes", "cwd": "/Users/you/code/my-app", "project": "hermes·desktop",
         "elapsed": "38:12", "cpu": "1.7", "task": "修 dashboard 的交互"},
    ]


def serve(port, style, refresh, demo, lang="zh", days=14, rows=20, graph_n=12,
          tdays=7, projs=6, scope="all", sdays=7, host="127.0.0.1"):
    """小 HTTP 服务：按需渲染，页面靠 JS 增量刷新（不是整页重载）。

    查询参数：?lang=zh|en &days=7|14|30 &rows=20|50|100 &graph=6|12|24 &scope=all|dg
             &tdays=7|14|30 &projs=4|6|10 &sdays=7|14|30
    增量刷新：?frag=manifest 回各块哈希；?frag=<块名> 回那一块的 HTML。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    memo = {}
    recs_memo = {"t": 0.0, "recs": None}
    procs_memo = {"t": 0.0, "procs": [], "pid": None}

    def get_recs(lang_, force=False):
        """采集结果短时复用：切范围时只重渲染（~30ms），不再重解全部会话（~1.3s）。
        force=True 时绕过复用（数据源指纹变了，必须重新采集）。"""
        if demo:
            return demo_recs(lang_)
        now = time.time()
        if not force and recs_memo["recs"] is not None and now - recs_memo["t"] < 4.0:
            return recs_memo["recs"]
        recs = W.collect()
        recs_memo.update({"t": time.time(), "recs": recs})
        return recs

    def get_procs(lang_):
        """进程列表 15s 复用：ps 扫描约 0.3s，比整页渲染还贵。"""
        if demo:
            return demo_procs(lang_)
        now = time.time()
        if procs_memo["pid"] == os.getpid() and now - procs_memo["t"] < 15.0:
            return procs_memo["procs"]
        procs = W.scan_processes()
        procs_memo.update({"t": time.time(), "procs": procs, "pid": os.getpid()})
        return procs

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/favicon"):
                self.send_response(204)
                self.end_headers()
                return
            q = parse_qs(urlparse(self.path).query)

            def pick(key, allowed, default):
                v = (q.get(key) or [str(default)])[0]
                return v if v in [str(a) for a in allowed] else str(default)

            want_frag = (q.get("frag") or [""])[0]
            want_static = "static=1" in self.path          # 截图/快照：不轮询
            want_lang = pick("lang", ["zh", "en"], lang)
            want_days = int(pick("days", [7, 14, 30], days))
            want_rows = int(pick("rows", [20, 50, 100], rows))
            want_graph = int(pick("graph", [6, 12, 24], graph_n))
            want_tdays = int(pick("tdays", [7, 14, 30], tdays))
            want_projs = int(pick("projs", [4, 6, 10], projs))
            want_scope = pick("scope", ["all", "dg"], scope)
            want_sdays = int(pick("sdays", [7, 14, 30], sdays))
            want_style = "clean" if "style=clean" in self.path else style
            key = (want_style, want_lang, want_days, want_rows, want_graph, want_tdays, want_projs,
                   want_scope, want_sdays, want_static)

            # 「只有变化才刷新」：先比数据源指纹（只 stat），没变就复用上次渲染结果。
            sig = "demo" if demo else src_sig()
            ent = memo.get(key)
            stale = (ent is None or ent["sig"] != sig or
                     time.time() - ent["ts"] > 30)          # 30s 兜底：进程增减不一定写文件
            if stale:
                recs = get_recs(want_lang, force=bool(ent and ent["sig"] != sig))
                procs = get_procs(want_lang)
                cur = {"lang": want_lang, "days": want_days, "rows": want_rows, "graph": want_graph,
                       "tdays": want_tdays, "projs": want_projs, "scope": want_scope,
                       "sdays": want_sdays}
                if want_style == "clean":
                    doc = render(recs, 0 if want_static else refresh)
                else:
                    doc = render_pixel(recs, 0 if want_static else refresh, lang=want_lang, days=want_days,
                                       rows=want_rows, graph_n=want_graph, procs=procs, cur=cur,
                                       tdays=want_tdays, projs=want_projs, scope=want_scope,
                                       sdays=want_sdays)
                frags, hashes = split_frags(doc)
                v = hashlib.sha1("".join(hashes[k] for k in sorted(hashes)).encode()).hexdigest()[:12] \
                    if hashes else hashlib.sha1(doc.encode("utf-8")).hexdigest()[:12]
                ent = {"ts": time.time(), "sig": sig, "doc": doc.encode("utf-8"),
                       "frags": frags, "hashes": hashes, "v": v}
                memo[key] = ent

            if want_frag == "manifest":
                body = json.dumps({"v": ent["v"], "frags": ent["hashes"], "poll": refresh,
                                   "hints": FRAG_HINTS}, ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            elif want_frag:
                frag = ent["frags"].get(want_frag)
                if frag is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = frag.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            else:
                body = ent["doc"]
                ctype = "text/html; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    print(f"http://{shown}:{port}/  (style={style}, lang={lang}, refresh={refresh}s"
          f"{', demo' if demo else ''})")
    if host in ("0.0.0.0", "::"):
        print(f"局域网访问：http://<本机局域网 IP>:{port}/   （绑定 {host}:{port}）")
    srv.serve_forever()


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--refresh", type=int, default=5)
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--style", choices=["clean", "pixel"], default="clean")
    ap.add_argument("--serve", type=int, metavar="PORT", help="起 HTTP 服务，每次请求重新采集（页面 5s 自动刷新）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址：127.0.0.1=仅本机（默认）；0.0.0.0=局域网可访问")
    ap.add_argument("--demo", action="store_true", help="用虚构演示数据（截图/试用，不读本机会话）")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh", help="界面语言（默认 zh）")
    ap.add_argument("--days", type=int, choices=[7, 14, 30], default=14, help="图表时间范围")
    ap.add_argument("--rows", type=int, choices=[20, 50, 100], default=20, help="流水表行数")
    ap.add_argument("--graph", type=int, choices=[6, 12, 24], default=12, help="拓扑里显示多少个任务")
    ap.add_argument("--tdays", type=int, choices=[7, 14, 30], default=7, help="时间线天数")
    ap.add_argument("--projs", type=int, choices=[4, 6, 10], default=6, help="项目图显示几个")
    ap.add_argument("--scope", choices=["all", "dg"], default="all",
                    help="时间线范围：all=含 Hermes 主循环，dg=只看派出去的任务")
    ap.add_argument("--sdays", type=int, choices=[7, 14, 30], default=7, help="切片面板天数")
    args = ap.parse_args()

    if args.serve:
        return serve(args.serve, args.style, 0 if args.no_refresh else args.refresh, args.demo,
                     lang=args.lang, days=args.days, rows=args.rows, graph_n=args.graph,
                     tdays=args.tdays, projs=args.projs, scope=args.scope, sdays=args.sdays,
                     host=args.host)

    recs = demo_recs(args.lang) if args.demo else W.collect()
    refresh = 0 if args.no_refresh else args.refresh
    if args.style == "pixel":
        procs = demo_procs(args.lang) if args.demo else W.scan_processes()
        doc = render_pixel(recs, refresh, lang=args.lang, days=args.days, rows=args.rows,
                           graph_n=args.graph, procs=procs, tdays=args.tdays, projs=args.projs,
                           scope=args.scope, sdays=args.sdays,
                           cur={"lang": args.lang, "days": args.days, "rows": args.rows,
                                "graph": args.graph, "tdays": args.tdays, "projs": args.projs,
                                "scope": args.scope, "sdays": args.sdays})
        out = args.out or PIXEL_CACHE
    else:
        doc = render(recs, refresh)
        out = args.out or CACHE
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(out)
    if args.open:
        os.system(f'open "{out}"')


if __name__ == "__main__":
    main()
