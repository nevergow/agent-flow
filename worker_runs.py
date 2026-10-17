#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker_runs.py — 看 Hermes 派出去的执行者（DSH headless / Pi）到底在跑什么。

数据来源（全部只读，不改任何东西）
================================
  DSH 会话   ~/.dsh/sessions/<cwd-slug>/session-<uuid>/session.v*.jsonl.zstd
             （zstd 压缩的 JSONL 事件流；v4 格式；含 usage 与 tool/call）
  DSH 日志   ~/.hermes/logs/dsh_<step>.log  （派活时我重定向的 stdout，便于 tail -f）
  Pi 一次性  ~/.pi/agent/sessions/<cwd-slug>/<ts>_<uuid>.jsonl
  Pi RPC     ~/.hermes/logs/pi-rpc-<name>.jsonl （pi_rpc.py 常驻会话的事件流）
  Pi RPC st  ~/.hermes/state/pi-rpc/<name>/     （会话是否还活着）

用法
====
  worker_runs.py                     # 最近 20 条（DSH + Pi 一起）
  worker_runs.py --worker dsh        # 只看 DSH / --worker pi 只看 Pi
  worker_runs.py --limit 50 --since 48h
  worker_runs.py show <KEY>          # 单条详情：任务书首段 + 工具调用时间线 + 最终回答
  worker_runs.py follow [KEY]        # 实时跟（不给 KEY = 跟最新 RUNNING 的那条），Ctrl-C 退出
  worker_runs.py --json              # 机器可读

KEY 是打印在表格第一列末尾的短标识（DSH 取 session id 前 8 位，Pi 取会话名/文件名）。
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
DSH_SESSIONS = os.path.join(HOME, ".dsh", "sessions")
DSH_LOGS = os.path.join(HOME, ".hermes", "logs")
PI_SESSIONS = os.path.join(HOME, ".pi", "agent", "sessions")
PI_RPC_LOGS = os.path.join(HOME, ".hermes", "logs")
TZ = timezone(timedelta(hours=8))

# deepseek-flash 官方价（元/百万 token，空闲 / 高峰=双倍）
PRICE = {"in": 1.0, "out": 4.0, "cache": 0.02}


def _peak(ts):
    """北京工作日 9-12、14-18 为高峰（不含法定节假日，这里近似）。"""
    d = datetime.fromtimestamp(ts, TZ)
    if d.weekday() >= 5:
        return False
    h = d.hour + d.minute / 60
    return (9 <= h < 12) or (14 <= h < 18)


def _fmt_dur(sec):
    if sec is None or sec < 0:
        return "-"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _fmt_tok(n):
    if not n:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1e6:.1f}M"


def _fmt_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M:%S")


def _one_line(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return (s[:n] + "…") if len(s) > n else s


def _ms(v):
    """createdAt / time 可能是 epoch ms 或 ISO 串。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iter_dsh_events(path):
    """逐行产出 (event, 下一行是否已读) —— 用 zstd -dc 流式解码，不落临时文件。"""
    with subprocess.Popen(
        ["zstd", "-dc", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as p:
        for raw in p.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


# --------------------------------------------------------------------------- DSH

def scan_dsh():
    out = []
    for sd in glob.glob(os.path.join(DSH_SESSIONS, "*", "session-*")):
        files = sorted(
            glob.glob(os.path.join(sd, "session.v*.jsonl.zstd")),
            key=lambda p: int(re.search(r"v(\d+)", p).group(1)) if re.search(r"v(\d+)", p) else 0,
            reverse=True,
        )
        if not files:
            continue
        path = files[0]
        rec = {
            "worker": "DSH",
            "path": path,
            "key": os.path.basename(sd).replace("session-", "")[:8],
            "state": "?",
            "tokens": 0,
            "tok_in": 0,
            "tok_out": 0,
            "tok_cache": 0,
            "cost_cny": 0.0,
            "steps": 0,
            "turns": 0,
            "tools": {},
            "tool_calls": [],
            "title": "",
            "task": "",
            "last_text": "",
            "reason": None,
            "created": 0.0,
            "last": os.path.getmtime(path),
        }
        last_ts = 0.0
        for e in _iter_dsh_events(path):
            t = e.get("type")
            d = e.get("data") or {}
            ts = _ms(e.get("time")) or rec["last"]
            last_ts = max(last_ts, ts)
            if t == "session":  # cwd / createdAt 在顶层，不在 data 里
                rec["cwd"] = e.get("cwd") or d.get("cwd") or ""
                rec["created"] = _ms(e.get("createdAt") or d.get("createdAt")) or ts
            elif t == "session/title":
                rec["title"] = d.get("title") or rec["title"]
            elif t == "user/message":
                if not rec["task"]:
                    for c in d.get("content") or []:
                        if c.get("type") == "text":
                            rec["task"] = c.get("text") or ""
                            break
            elif t == "turn/start":
                rec["turns"] += 1
            elif t == "step/start":
                rec["steps"] += 1
            elif t == "assistant/message":
                msg = d.get("message") or {}
                u = d.get("usage") or msg.get("usage") or {}
                if u:
                    peak = _peak(ts)
                    k = 2 if peak else 1
                    rec["tokens"] += int(u.get("totalTokens") or 0)
                    rec["tok_in"] += int(u.get("inputTokens") or 0)
                    rec["tok_out"] += int(u.get("outputTokens") or 0)
                    rec["tok_cache"] += int(u.get("cacheReadTokens") or 0)
                    rec["cost_cny"] += k * (
                        (int(u.get("inputTokens") or 0) * PRICE["in"]
                         + int(u.get("outputTokens") or 0) * PRICE["out"]
                         + int(u.get("cacheReadTokens") or 0) * PRICE["cache"])
                        / 1e6
                    )
                texts = [c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text"]
                if any(x.strip() for x in texts):
                    rec["last_text"] = "\n".join(x for x in texts if x.strip())
            elif t == "tool/call":
                name = d.get("name") or "?"
                rec["tools"][name] = rec["tools"].get(name, 0) + 1
                rec["tool_calls"].append((ts, name, _one_line(d.get("arguments"), 140)))
            elif t == "turn/end":
                rec["reason"] = (d.get("reason") or {}).get("kind")
        if not rec.get("cwd"):
            rec["cwd"] = os.path.basename(os.path.dirname(sd)).replace("--", "/").replace("-", "/")
        rec["project"] = os.path.basename(rec["cwd"]) or "?"
        if last_ts:
            rec["last"] = last_ts
        age = time.time() - rec["last"]
        if age < 120 and rec["reason"] is None:
            rec["state"] = "RUNNING"
        elif rec["reason"] == "completed":
            rec["state"] = "done"
        elif rec["reason"] in ("aborted", "cancelled"):
            rec["state"] = "aborted"
        elif rec["reason"] in ("error", "failed"):
            rec["state"] = "FAILED"
        elif rec["last"] and rec["reason"] is None:
            rec["state"] = "interrupted"
        rec["start"] = rec["created"] or rec["last"]
        rec["end"] = rec["last"] if rec["state"] != "RUNNING" else None
        rec["label"] = rec["title"] or rec["task"]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------- Pi

def _pi_usage_totals(msg):
    u = (msg or {}).get("usage") or {}
    tok = int(u.get("totalTokens") or 0)
    cost = float(((u.get("cost") or {}).get("total") or 0.0))
    return tok, cost


def scan_pi_sessions():
    out = []
    for path in glob.glob(os.path.join(PI_SESSIONS, "*", "*.jsonl")):
        rec = {
            "worker": "Pi",
            "path": path,
            "key": os.path.basename(path).split("_")[-1].replace(".jsonl", "")[:8],
            "state": "?",
            "tokens": 0,
            "tok_in": 0,
            "tok_out": 0,
            "tok_cache": 0,
            "cost_usd": 0.0,
            "title": "",
            "task": "",
            "last_text": "",
            "reason": None,
            "created": 0.0,
            "last": os.path.getmtime(path),
            "tool_calls": [],
            "tools": {},
            "steps": 0,
            "turns": 0,
        }
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t == "session":
                    rec["cwd"] = e.get("cwd") or ""
                    rec["created"] = _ms(e.get("timestamp"))
                elif t == "session_info":
                    rec["title"] = e.get("name") or rec["title"]
                elif t == "message":
                    msg = e.get("message") or {}
                    role = msg.get("role")
                    ts = _ms(e.get("timestamp")) or rec["last"]
                    if role == "user":
                        content = msg.get("content")
                        txt = content if isinstance(content, str) else " ".join(
                            c.get("text", "") for c in content or [] if isinstance(c, dict)
                        )
                        if not rec["task"]:
                            rec["task"] = txt
                        rec["turns"] += 1
                    elif role == "assistant":
                        tok, cost = _pi_usage_totals(msg)
                        rec["tokens"] += tok
                        rec["tok_in"] += int((msg.get("usage") or {}).get("input") or 0)
                        rec["tok_out"] += int((msg.get("usage") or {}).get("output") or 0)
                        rec["tok_cache"] += int((msg.get("usage") or {}).get("cacheRead") or 0)
                        rec["cost_usd"] += cost
                        if (msg.get("stopReason") or "") == "stop":
                            rec["ended"] = True
                        content = msg.get("content") or []
                        txt = []
                        for c in content if isinstance(content, list) else []:
                            if c.get("type") == "text":
                                txt.append(c.get("text", ""))
                            elif c.get("type") == "toolCall":
                                name = c.get("name") or "?"
                                rec["tools"][name] = rec["tools"].get(name, 0) + 1
                                rec["tool_calls"].append(
                                    (ts, name, _one_line(json.dumps(c.get("arguments")), 140))
                                )
                        if txt:
                            rec["last_text"] = "\n".join(txt)
                elif t == "tool_execution_start":
                    pass
        except OSError:
            continue
        rec.setdefault("cwd", "")
        rec["project"] = os.path.basename(rec["cwd"]) or "?"
        rec["state"] = "done" if rec.get("ended") else "interrupted"
        rec["start"] = rec["created"] or rec["last"]
        rec["end"] = rec["last"]
        rec["label"] = rec["title"] or rec["task"]
        rec["kind"] = "one-shot"
        out.append(rec)
    return out


def scan_pi_rpc():
    out = []
    for path in glob.glob(os.path.join(PI_RPC_LOGS, "pi-rpc-*.jsonl")):
        name = re.sub(r"^pi-rpc-|\.jsonl$", "", os.path.basename(path))
        rec = {
            "worker": "Pi·rpc",
            "path": path,
            "key": name,
            "state": "?",
            "tokens": 0,
            "tok_in": 0,
            "tok_out": 0,
            "tok_cache": 0,
            "cost_usd": 0.0,
            "title": name,
            "task": "",
            "last_text": "",
            "created": os.path.getctime(path),
            "last": os.path.getmtime(path),
            "tool_calls": [],
            "tools": {},
            "steps": 0,
            "turns": 0,
            "kind": "rpc",
        }
        settled = 0
        ts_first = 0.0
        ts_last = 0.0
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                mts = _ms((e.get("message") or {}).get("timestamp")) or _ms(e.get("timestamp"))
                if mts:
                    ts_first = ts_first or mts
                    ts_last = max(ts_last, mts)
                if t == "message_start" or t == "message_end" or t == "turn_end":
                    msg = e.get("message") or {}
                    role = msg.get("role")
                    ts = _ms(e.get("timestamp")) or rec["last"]
                    if t == "turn_end" and role == "assistant":
                        tok, cost = _pi_usage_totals(msg)
                        rec["tokens"] += tok
                        rec["tok_in"] += int((msg.get("usage") or {}).get("input") or 0)
                        rec["tok_out"] += int((msg.get("usage") or {}).get("output") or 0)
                        rec["tok_cache"] += int((msg.get("usage") or {}).get("cacheRead") or 0)
                        rec["cost_usd"] += cost
                        rec["turns"] += 1
                        txt = " ".join(
                            c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text"
                        )
                        if txt.strip():
                            rec["last_text"] = txt
                    if role == "user":
                        content = msg.get("content")
                        txt = content if isinstance(content, str) else " ".join(
                            c.get("text", "") for c in content or [] if isinstance(c, dict)
                        )
                        if txt.strip():
                            rec["task"] = txt
                elif t == "tool_execution_start":
                    ts = _ms(e.get("timestamp")) or rec["last"]
                    nm = e.get("toolName") or e.get("name") or "?"
                    rec["tools"][nm] = rec["tools"].get(nm, 0) + 1
                    rec["tool_calls"].append((ts, nm, _one_line(json.dumps(e.get("args") or e.get("input")), 140)))
                elif t == "agent_settled":
                    settled += 1
                elif t == "agent_start":
                    rec["last_start"] = _ms(e.get("timestamp")) or rec["last"]
        except OSError:
            continue
        alive = os.path.exists(path) and (time.time() - rec["last"] < 90)
        rec["state"] = "RUNNING" if alive else ("done" if settled else "interrupted")
        rec["project"] = f"pi-rpc:{name}"[:20]
        rec["start"] = ts_first or rec["created"]
        rec["end"] = None if rec["state"] == "RUNNING" else (ts_last or rec["last"])
        out.append(rec)
    return out


# -------------------------------------------------------------------------- 输出

def collect(workers):
    recs = []
    if "dsh" in workers:
        recs += scan_dsh()
    if "pi" in workers:
        recs += scan_pi_sessions() + scan_pi_rpc()
    recs.sort(key=lambda r: r["start"], reverse=True)
    return recs


def _dur(rec):
    end = rec.get("end") or time.time()
    return max(0.0, end - (rec.get("start") or end))


def print_table(recs, limit, show_cost=True):
    hdr = f"{'TIME':<15} {'DUR':>7}  {'WORKER':<7} {'PROJECT':<20} {'STATE':<11} {'TOKENS':>7} {'¥/$':>8}  {'KEY':<9} TASK"
    print(hdr)
    print("-" * len(hdr))
    for r in recs[:limit]:
        cost = ""
        if r.get("worker") == "DSH":
            cost = f"≈{r['cost_cny']:.2f}"
        else:
            cost = f"${r['cost_usd']:.4f}" if r.get("cost_usd") else "-"
        print(
            f"{_fmt_time(r['start']):<15} {_fmt_dur(_dur(r)):>7}  {r['worker']:<7} "
            f"{(r.get('project') or '-')[:20]:<20} {r['state']:<11} {_fmt_tok(r.get('tokens')):>7} "
            f"{cost:>8}  {r['key']:<9} {_one_line(r.get('label'), 60)}"
        )
    if show_cost:
        print("\n≈¥ = 按 deepseek-flash 官方价（空闲 ¥1/¥4、高峰双倍）对会话内 usage 估算；$ = pi 自报成本。")


def show(rec):
    print(f"KEY      {rec['key']}   [{rec['worker']}]  状态 {rec['state']}")
    print(f"项目     {rec.get('cwd') or rec.get('project')}")
    print(f"开始     {_fmt_time(rec['start'])}   耗时 {_fmt_dur(_dur(rec))}")
    print(f"tokens   {rec.get('tokens')}   " + (
        f"≈¥{rec['cost_cny']:.3f}" if rec['worker'] == 'DSH' else f"${rec.get('cost_usd', 0):.4f}"))
    if rec.get("tools"):
        top = ", ".join(f"{k}×{v}" for k, v in sorted(rec["tools"].items(), key=lambda kv: -kv[1]))
        print(f"工具     {top}")
    print(f"事件流   {rec['path']}")
    print(f"\n—— 任务（首条 user 消息）——\n{_one_line(rec.get('task'), 600)}\n")
    calls = rec.get("tool_calls") or []
    if calls:
        print(f"—— 工具调用时间线（共 {len(calls)}，最后 15 条）——")
        for ts, name, arg in calls[-15:]:
            print(f"  {datetime.fromtimestamp(ts, TZ).strftime('%H:%M:%S')}  {name:<14} {arg}")
        print()
    if rec.get("last_text"):
        print(f"—— 最后一段助手输出 ——\n{rec['last_text'][:1500]}")


def follow(rec, poll=2.0):
    """实时跟一条：DSH 每次重解压取新 seq；Pi 按字节偏移 tail。"""
    print(f"跟随 {rec['key']}（{rec['worker']}）— Ctrl-C 退出\n")
    if rec["state"] != "RUNNING":
        print(f"这条已经不是运行态（{rec['state']}，{_fmt_time(rec['start'])}，{_fmt_dur(_dur(rec))}）。")
        print("想看它当时干了什么：worker_runs.py show " + rec["key"])
        print("想看最后的调用：")
        for ts, name, arg in (rec.get("tool_calls") or [])[-8:]:
            print(f"  {datetime.fromtimestamp(ts, TZ):%m-%d %H:%M:%S}  {name:<14} {arg}")
        return
    is_dsh = rec["worker"] == "DSH"
    seen = 0
    off = 0
    if is_dsh:
        seen = sum(1 for _ in _iter_dsh_events(rec["path"]))
        print(f"（已有 {seen} 条事件，从这里往后跟）\n", flush=True)
    else:
        off = os.path.getsize(rec["path"])
    while True:
        try:
            if is_dsh:
                n = 0
                for e in _iter_dsh_events(rec["path"]):
                    n += 1
                    if n <= seen:
                        continue
                    t = e.get("type")
                    d = e.get("data") or {}
                    ts = _ms(e.get("time")) or time.time()
                    if t == "tool/call":
                        print(f"{datetime.fromtimestamp(ts, TZ):%H:%M:%S}  ⚙ {d.get('name')}  {_one_line(d.get('arguments'), 120)}")
                    elif t == "assistant/message":
                        txt = " ".join(c.get("text", "") for c in (d.get("message") or {}).get("content") or [] if c.get("type") == "text")
                        if txt.strip():
                            print(f"{datetime.fromtimestamp(ts, TZ):%H:%M:%S}  💬 {_one_line(txt, 300)}")
                    elif t == "turn/end":
                        print(f"{datetime.fromtimestamp(ts, TZ):%H:%M:%S}  ✔ turn 结束：{(d.get('reason') or {}).get('kind')}")
                        seen = n
                        return
                seen = n
            else:
                with open(rec["path"], "rb") as f:
                    f.seek(off)
                    chunk = f.read()
                    off = f.tell()
                for line in chunk.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = e.get("type")
                    if t == "tool_execution_start":
                        print(f"  ⚙ {e.get('toolName') or e.get('name')}  {_one_line(json.dumps(e.get('args') or e.get('input')), 120)}")
                    elif t == "turn_end":
                        msg = e.get("message") or {}
                        txt = " ".join(c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text")
                        if txt.strip():
                            print(f"  💬 {_one_line(txt, 300)}")
                    elif t == "agent_settled":
                        print("  ✔ agent_settled（本轮结束）")
                        return
        except KeyboardInterrupt:
            return
        except FileNotFoundError:
            print("事件流不见了（会话被删/被清理）")
            return
        time.sleep(poll)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="看 DSH / Pi 执行者在跑什么")
    ap.add_argument("cmd", nargs="?", default="list", choices=["list", "show", "follow"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("--worker", choices=["all", "dsh", "pi"], default="all")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--since", default=None, help="如 24h / 3d")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    workers = {"dsh": ["dsh"], "pi": ["pi"], "all": ["dsh", "pi"]}[args.worker]
    recs = collect(workers)

    if args.since:
        m = re.fullmatch(r"(\d+)([hd])", args.since)
        if not m:
            sys.exit("--since 格式: 24h / 3d")
        cut = time.time() - int(m.group(1)) * (3600 if m.group(2) == "h" else 86400)
        recs = [r for r in recs if r["start"] >= cut]

    if args.cmd == "list":
        if args.json:
            print(json.dumps([{k: v for k, v in r.items() if k != "tool_calls"} for r in recs[: args.limit]],
                             ensure_ascii=False, indent=2, default=str))
        else:
            print_table(recs, args.limit)
        return

    if not recs:
        sys.exit("没有任何运行记录")

    if args.cmd == "show":
        if not args.key:
            sys.exit("用法: worker_runs.py show <KEY>")
        hits = [r for r in recs if r["key"].startswith(args.key)]
        if not hits:
            sys.exit(f"没找到 key={args.key}（用 worker_runs.py 看表格里的 KEY 列）")
        show(hits[0])
        return

    if args.cmd == "follow":
        target = None
        if args.key:
            hits = [r for r in recs if r["key"].startswith(args.key)]
            if not hits:
                sys.exit(f"没找到 key={args.key}")
            target = hits[0]
        else:
            running = [r for r in recs if r["state"] == "RUNNING"]
            if not running:
                sys.exit("当前没有 RUNNING 的执行者；给个 KEY 跟历史会话的最后状态")
            target = running[0]
        follow(target)


if __name__ == "__main__":
    main()
