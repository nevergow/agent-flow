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
import copy
import glob
import json
import os
import re
import sqlite3
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

# deepseek-flash 官方价（元/百万 token；空闲价，高峰=双倍）
# 缓存命中 ¥0.02 / 输入未命中 ¥1 / 输出 ¥4 —— 见 api-docs.deepseek.com/quick_start/pricing
PRICE = {"in": 1.0, "out": 4.0, "cache": 0.02}
FX = 7.1  # 美元折算汇率，只用于把上游自报的 $ 估算并到人民币口径


def _peak(ts):
    """北京工作日 9-12、14-18 为高峰（不含法定节假日，这里近似）。"""
    d = datetime.fromtimestamp(ts, TZ)
    if d.weekday() >= 5:
        return False
    h = d.hour + d.minute / 60
    return (9 <= h < 12) or (14 <= h < 18)


def cost_cny_of(tin, tout, tcache, ts):
    """统一口径的 token 花费估算（元）：六种执行者用同一张表，避免"谁的 $ 谁的 ¥"打架。

    ts 只用来判高峰/空闲；跨时段的长会话按开始时刻近似。
    """
    k = 2 if _peak(ts or time.time()) else 1
    return round(k * (int(tin or 0) * PRICE["in"]
                      + int(tout or 0) * PRICE["out"]
                      + int(tcache or 0) * PRICE["cache"]) / 1e6, 6)


IDLE_GAP = 20 * 60  # 空闲超过 20 分钟算两段活动


def activity_segments(times, running=False, gap=IDLE_GAP):
    """把事件时间戳压成"活跃区间"：[["s","e"], ...]。

    会话会被恢复/复用（DSH 会话 10-04 建的，10-17 又聊了几句），按 created→last
    画就是一条 13 天的假长条；只有按活跃区间画，时间线才回答"什么时候真的在跑"。
    """
    segs = []
    for t in sorted(x for x in times if x):
        if segs and t - segs[-1][1] <= gap:
            segs[-1][1] = max(segs[-1][1], t)
        else:
            segs.append([t, t])
    if running and segs:
        segs[-1][1] = max(segs[-1][1], time.time())
    return segs


def seg_seconds(segs):
    return sum(max(0.0, b - a) for a, b in (segs or []))



# ---------------------------------------------------------------- 单文件解析缓存
# 看板每 5s 自刷、切范围时都会重跑 collect()。会话文件只有少数在变，
# 按 (mtime,size) 缓存单文件解析结果，避免每次重解 30MB 的 pi-rpc 日志。
_PARSE_CACHE = {}
_PARSE_CACHE_MAX = 512


def _parse_cached(path, parser):
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (round(st.st_mtime, 3), st.st_size)
    ent = _PARSE_CACHE.get(path)
    if ent and ent[0] == sig:
        return copy.deepcopy(ent[1])
    rec = parser()
    if rec is None:
        return None
    _PARSE_CACHE[path] = (sig, copy.deepcopy(rec))
    if len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
        for k in list(_PARSE_CACHE)[: len(_PARSE_CACHE) - _PARSE_CACHE_MAX]:
            _PARSE_CACHE.pop(k, None)
    return rec


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

def _finish_dsh(rec):
    """状态/起止时间随时钟变化，缓存命中的 rec 每次都要重算这一段。"""
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
    return _extend_running(rec)


def _extend_running(rec):
    """RUNNING 的会话：最后一段活跃区间延伸到此刻（否则条子停在最后一次事件上）。"""
    if rec.get("state") == "RUNNING" and rec.get("segs"):
        rec["segs"][-1][1] = max(rec["segs"][-1][1], time.time())
    return rec


def _parse_dsh(sd, path):
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
    times = []
    for e in _iter_dsh_events(path):
        t = e.get("type")
        d = e.get("data") or {}
        ts = _ms(e.get("time")) or rec["last"]
        last_ts = max(last_ts, ts)
        times.append(ts)
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
    rec["cost_cny"] = round(rec["cost_cny"], 6)
    rec["segs"] = activity_segments(times)
    return rec


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
        rec = _parse_cached(path, lambda sd=sd, path=path: _parse_dsh(sd, path))
        if rec is None:
            continue
        out.append(_finish_dsh(rec))
    return out


# ---------------------------------------------------------------------------- Pi

def _pi_usage_totals(msg):
    u = (msg or {}).get("usage") or {}
    tok = int(u.get("totalTokens") or 0)
    cost = float(((u.get("cost") or {}).get("total") or 0.0))
    return tok, cost


def _parse_pi_session(path):
    rec = {
        "worker": "Pi",
        "path": path,
        "key": re.sub(r"[^0-9a-z]", "", os.path.splitext(os.path.basename(path))[0].split("_")[-1])[-8:],
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
    times = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            mts = _ms(e.get("timestamp"))
            if mts:
                times.append(mts)
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
                        rec.pop("error", None)
                    elif (msg.get("stopReason") or "") == "error":
                        rec["error"] = str(msg.get("errorMessage") or "error")
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
        return None
    rec.setdefault("cwd", "")
    rec["project"] = os.path.basename(rec["cwd"]) or "?"
    rec["state"] = "done" if rec.get("ended") else "interrupted"
    rec["start"] = rec["created"] or rec["last"]
    rec["end"] = rec["last"]
    rec["label"] = rec["title"] or rec["task"]
    rec["kind"] = "one-shot"
    rec["cost_cny"] = cost_cny_of(rec["tok_in"], rec["tok_out"], rec["tok_cache"], rec["start"])
    rec["segs"] = activity_segments(times)
    return rec


def scan_pi_sessions(procs=None):
    procs = live_pi_procs() if procs is None else procs
    out = []
    for path in glob.glob(os.path.join(PI_SESSIONS, "*", "*.jsonl")):
        rec = _parse_cached(path, lambda path=path: _parse_pi_session(path))
        if rec is not None:
            out.append(_finish_pi_session(rec, procs))
    return out


def _pid_alive(pid):
    """进程是否还活着。os.kill(pid, 0) 不发信号只做存在性检查。"""
    try:
        return os.kill(int(pid), 0) is None
    except (OSError, ValueError, TypeError):
        return False


def _etime_sec(s):
    """ps 的 etime（[[D-]HH:]MM:SS）→ 秒。"""
    s = (s or "").strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    try:
        parts = [int(x) for x in s.split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + sec


def live_pi_procs():
    """当前活着的 Pi 进程（一次性 + 常驻 RPC 都在内）。

    pi 的 cmdline 在 ps 里就是一个词 `pi`（npm 全局 bin 直接 exec），
    所以必须按\"整词\"匹配——以前只认 `pi -p ...`，结果一个都扫不到。
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pcpu=,etime=,command="],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return []
    now = time.time()
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, etime, cmd = parts
        head = cmd.strip()
        if not re.match(r"^(\S*/)?pi(\s|$)", head):
            continue
        rows.append({"pid": int(pid), "cpu": cpu,
                     "start": now - _etime_sec(etime), "cwd": _pid_cwd(pid)})
    return rows


def _rpc_meta(name):
    path = os.path.join(HOME, ".hermes", "state", "pi-rpc", name, "meta.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _finish_pi_session(rec, procs=None):
    """一次性 Pi 会话的存活判定。

    会话文件里没有\"进程还活着\"的字段：ended 只在最后一条 assistant 消息
    stopReason=stop 时出现，所以以前任何运行中的会话都被读成\"中断\"。
    现在用真实 pi 进程（cwd + 启动时刻最近）对齐，配 mtime 兜底。
    """
    procs = live_pi_procs() if procs is None else procs
    cands = [p for p in procs if p.get("cwd") and p["cwd"] == rec.get("cwd")]
    best = min(cands, key=lambda p: abs(p["start"] - rec["start"])) if cands else None
    fresh = (time.time() - rec["last"]) < 90
    # 有 pi 进程在跑却不在这个 cwd → 这次会话肯定没在跑（ps 失败时才退回 mtime 判定）
    running_now = best or (not procs and fresh)
    if (not rec.get("ended")) and (not rec.get("error")) and running_now:
        rec["state"] = "RUNNING"
        rec["end"] = None
        if best:
            rec["pid"] = best["pid"]
    else:
        if rec.get("ended"):
            rec["state"] = "done"
        elif rec.get("error"):
            rec["state"] = "FAILED"
        else:
            rec["state"] = "interrupted"
        rec["end"] = rec["last"]
    return _extend_running(rec)


def _finish_pi_rpc(rec, settled, name, path):
    """存活判定依赖当前时间与进程状态，缓存命中的记录每次都要重算。"""
    meta = _rpc_meta(name)
    alive = any(_pid_alive(meta.get(k)) for k in ("pid", "holder_pid") if meta.get(k))
    # 一轮在飞 = 最近一次 agent_start 之后还没有 agent_settled
    busy = (rec.get("last_start") or 0) > (rec.get("settled_ts") or 0)
    if alive and busy:
        rec["state"] = "RUNNING"
        rec["end"] = None
    elif rec.get("turn_error"):
        rec["state"] = "FAILED"       # 上一轮是 401/超时这类错误，别报成"完成"
        rec["end"] = rec.get("ts_last") or rec["last"]
    else:
        rec["state"] = "done" if settled else "interrupted"
        rec["end"] = rec.get("ts_last") or rec["last"]
    if meta.get("pid"):
        rec["pid"] = meta["pid"]
    rec["project"] = f"pi-rpc:{name}"[:20]
    rec["label"] = _one_line(rec.get("task") or "", 100) or name
    rec.pop("ts_last", None)
    rec["cost_cny"] = cost_cny_of(rec["tok_in"], rec["tok_out"], rec["tok_cache"], rec["start"])
    return _extend_running(rec)


def _parse_pi_rpc(path, name):
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
        "settled": 0,
    }
    ts_first = 0.0
    ts_last = 0.0
    times = []
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
                times.append(mts)
            if t == "message_start" or t == "message_end" or t == "turn_end":
                msg = e.get("message") or {}
                role = msg.get("role")
                ts = _ms(e.get("timestamp")) or rec["last"]
                if t == "turn_end" and role == "assistant":
                    tok, cost = _pi_usage_totals(msg)
                    if (msg.get("stopReason") or "") == "error":
                        rec["turn_error"] = str(msg.get("errorMessage") or "error")
                    else:
                        rec.pop("turn_error", None)
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
                    if txt.strip() and not rec["task"]:
                        rec["task"] = txt      # 只记第一次派活，后续 steer 不覆盖标题
            elif t == "tool_execution_start":
                ts = _ms(e.get("timestamp")) or rec["last"]
                nm = e.get("toolName") or e.get("name") or "?"
                rec["tools"][nm] = rec["tools"].get(nm, 0) + 1
                rec["tool_calls"].append((ts, nm, _one_line(json.dumps(e.get("args") or e.get("input")), 140)))
            elif t == "agent_settled":
                rec["settled"] += 1
                rec["settled_ts"] = _ms(e.get("timestamp")) or rec["last"]
            elif t == "agent_start":
                rec["last_start"] = _ms(e.get("timestamp")) or rec["last"]
    except OSError:
        return None
    rec["start"] = ts_first or rec["created"]
    rec["ts_last"] = ts_last
    rec["cost_usd"] = round(rec["cost_usd"], 6)
    rec["segs"] = activity_segments(times)
    return rec


def scan_pi_rpc():
    out = []
    for path in glob.glob(os.path.join(PI_RPC_LOGS, "pi-rpc-*.jsonl")):
        name = re.sub(r"^pi-rpc-|\.jsonl$", "", os.path.basename(path))
        rec = _parse_cached(path, lambda path=path, name=name: _parse_pi_rpc(path, name))
        if rec is None:
            continue
        settled = rec.pop("settled", 0)
        out.append(_finish_pi_rpc(rec, settled, name, path))
    return out


# -------------------------------------------------------------------------- 输出

def scan_processes():
    """当前真正在跑的 agent OS 进程（只读 ps / lsof），给看板的 PROCESSES 面板用。

    返回 [{pid, agent, cwd, project, elapsed, cpu, task}]，按 CPU 降序。
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pcpu=,etime=,command="],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return []
    pi_live = live_pi_procs()
    pi_start = {p["pid"]: p["start"] for p in pi_live}
    rpc_names = _pi_rpc_names_by_pid()
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, etime, cmd = parts
        if "worker_dashboard" in cmd or "scan_processes" in cmd:
            continue
        if cmd.startswith("sh -c "):      # pnpm/sh 包装层，真正的 node 进程另有一行
            continue
        agent, task = None, ""
        if "bin.ts headless" in cmd:
            agent = "DSH"
            m = re.search(r"bin\.ts headless\s+(.+)$", cmd)
            task = (m.group(1).strip() if m else "")[:60]
        elif "pi_rpc.py" in cmd:
            continue      # 客户端脚本（发一条就走），真正在跑的是它拉起的 pi
        elif re.match(r"^(\S*/)?pi(\s|$)", cmd.strip()):
            # pi 在 ps 里的 cmdline 就是一个词，不分 RPC/一次性就只能靠 meta 的 pid 认
            agent = "Pi·rpc" if int(pid) in rpc_names else "Pi"
            task = rpc_names.get(int(pid), "")
        elif "bundle/cli.js" in cmd:
            agent, task = "Pi", ""
        elif "codex" in cmd and "app-server" not in cmd and "happy" not in cmd:
            agent, task = "Codex", ""
        elif re.search(r"(^|\s)(claude|aider|gemini|opencode)(\s+-|\s*$)", cmd) and "grep" not in cmd and ".app/" not in cmd:
            agent, task = os.path.basename(cmd.split()[0]), ""
        if not agent:
            continue
        cwd = _pid_cwd(pid)
        if agent == "Pi" and not task:
            task = _pi_task_near(cwd, pi_start.get(int(pid)))
        rows.append({"pid": int(pid), "agent": agent, "cwd": cwd,
                     "project": os.path.basename(cwd.rstrip("/")) if cwd else "?",
                     "elapsed": etime, "cpu": cpu, "task": task})
    rows.sort(key=lambda r: -float(r["cpu"] or 0))
    return rows


def _pi_rpc_names_by_pid():
    """pid → 常驻会话名（meta.json 里登记了 pi 和 fifo holder 两个 pid）。"""
    m = {}
    for path in glob.glob(os.path.join(HOME, ".hermes", "state", "pi-rpc", "*", "meta.json")):
        name = os.path.basename(os.path.dirname(path))
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for k in ("pid", "holder_pid"):
            if d.get(k):
                m[int(d[k])] = name
    return m


def _pi_task_near(cwd, start):
    """一次性 pi 进程 → 它在跑的会话（同 cwd、启动时刻最近），取任务首行。"""
    if not cwd:
        return ""
    slug = "--" + cwd.strip("/").replace("/", "-") + "--"
    best, bestd = None, 1e18
    for path in glob.glob(os.path.join(PI_SESSIONS, slug, "*.jsonl")):
        rec = _parse_cached(path, lambda path=path: _parse_pi_session(path))
        if not rec or not rec.get("start"):
            continue
        d = abs(rec["start"] - (start or 0))
        if d < bestd:
            best, bestd = rec, d
    if best and bestd < 600:
        return _one_line(best.get("task") or best.get("label") or "", 60)
    return ""


def hermes_pid():
    """Hermes 网关进程的 PID（会话就是它跑的），找不到返回 None。"""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "hermes_cli.main" in line and "grep" not in line:
            return int(line.strip().split(None, 1)[0])
    return None


def _pid_cwd(pid):
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:].strip()
    return ""



HERMES_DB = os.path.join(HOME, ".hermes", "state.db")
CODEX_DIR = os.path.join(HOME, ".codex", "sessions")


def scan_hermes(days=7, limit=40):
    """Hermes 主循环会话（desktop / tui / cron）→ rec，token 与费用来自 session_model_usage。"""
    cut = time.time() - days * 86400
    out = []
    try:
        con = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            select s.id, s.source, s.display_name, s.model, s.started_at, s.ended_at, s.end_reason,
                   s.message_count,
                   coalesce(sum(u.input_tokens), 0) tin,
                   coalesce(sum(u.output_tokens), 0) tout,
                   coalesce(sum(u.cache_read_tokens), 0) tcache,
                   coalesce(sum(u.estimated_cost_usd), 0) usd,
                   coalesce(sum(u.api_call_count), 0) calls,
                   (select max(m.timestamp) from messages m where m.session_id = s.id) last_msg
            from sessions s left join session_model_usage u on u.session_id = s.id
            where s.started_at >= ?
            group by s.id order by s.started_at desc limit ?""", (cut, limit)).fetchall()
        # 每条会话的消息时间戳 → 活跃区间（"什么时候真的在跑"，而不是 created→last）
        mt = {}
        try:
            for m in con.execute("select session_id, timestamp from messages where timestamp >= ? "
                                 "order by timestamp", (cut,)):
                mt.setdefault(m["session_id"], []).append(m["timestamp"])
        except Exception:
            mt = {}
        con.close()
    except Exception:
        return out
    now = time.time()
    for r in rows:
        last = r["last_msg"] or r["started_at"] or 0
        live = r["ended_at"] is None and (now - last) < 180
        state = "RUNNING" if live else ("interrupted" if (r["end_reason"] or "").startswith(("interrupt", "abort")) else "done")
        label = (r["display_name"] or "").strip() or f'{r["source"]} session'
        out.append({
            "worker": "Hermes", "kind": "hermes", "key": r["id"], "path": "",
            "state": state, "tokens": int(r["tin"]) + int(r["tout"]) + int(r["tcache"]),
            "tok_in": int(r["tin"]), "tok_out": int(r["tout"]), "tok_cache": int(r["tcache"]),
            "cost_cny": cost_cny_of(r["tin"], r["tout"], r["tcache"], r["started_at"]),
            "cost_usd": round(float(r["usd"]), 6), "title": label, "task": label, "label": label,
            # ended_at 为空（中断/崩溃的旧会话）时用最后一条消息时间收口，
            # 否则 end=0 会让它看起来一直跑到"现在"——时间线和耗时都被拉成假长条
            "project": f'hermes·{r["source"]}', "start": r["started_at"],
            "end": r["ended_at"] or (last if state != "RUNNING" else 0.0),
            "segs": activity_segments(mt.get(r["id"]) or [r["started_at"], last],
                                      running=state == "RUNNING"),
            "turns": int(r["message_count"] or 0), "steps": 0, "tools": {}, "tool_calls": [],
            "last_text": "", "created": r["started_at"], "last": last, "last_start": last,
            "model": r["model"],
        })
    return out


def scan_hermes_subagents(days=7):
    """Hermes 派出的子 agent（async_delegations）→ rec。"""
    cut = time.time() - days * 86400
    out = []
    try:
        con = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            select delegation_id, state, dispatched_at, completed_at, task_json, result_json, owner_pid
            from async_delegations where dispatched_at >= ? order by dispatched_at desc""", (cut,)).fetchall()
        con.close()
    except Exception:
        return out
    for r in rows:
        tid = 0; tout = 0; usd = 0.0
        try:
            res = json.loads(r["result_json"] or "{}")
            for it in res.get("results") or []:
                tid += int(((it.get("tokens") or {}).get("input")) or 0)
                tout += int(((it.get("tokens") or {}).get("output")) or 0)
                usd += float(it.get("cost_usd") or 0)
        except Exception:
            pass
        try:
            goal = (json.loads(r["task_json"] or "{}").get("goal") or "").strip()
        except Exception:
            goal = ""
        st = {"completed": "done", "failed": "FAILED", "running": "RUNNING",
              "aborted": "interrupted", "cancelled": "interrupted"}.get(r["state"], "done")
        out.append({
            "worker": "Hermes·sub", "kind": "subagent", "key": r["delegation_id"], "path": "",
            "state": st, "tokens": tid + tout, "tok_in": tid, "tok_out": tout, "tok_cache": 0,
            "cost_cny": cost_cny_of(tid, tout, 0, r["dispatched_at"]),
            "segs": [[r["dispatched_at"], r["completed_at"] or r["dispatched_at"]]],
            "cost_usd": round(usd, 6), "title": goal[:120] or r["delegation_id"],
            "task": goal, "label": goal[:120] or r["delegation_id"], "project": "hermes·sub",
            "start": r["dispatched_at"], "end": r["completed_at"] or 0.0, "turns": 0, "steps": 0,
            "tools": {}, "tool_calls": [], "last_text": "", "created": r["dispatched_at"],
            "last": r["completed_at"] or r["dispatched_at"], "last_start": r["dispatched_at"],
            "pid": r["owner_pid"],
        })
    return out


def scan_codex(days=7):
    """Codex CLI rollout（~/.codex/sessions/**/*.jsonl）→ rec，token 取累计值逐次差值。"""
    cut = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        rec = {"cwd": "", "task": "", "start": 0.0, "end": 0.0, "tin": 0, "tout": 0, "tcache": 0,
               "usd": 0.0, "last_text": ""}
        times = []
        prev = None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = (e.get("timestamp") or 0)
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts = 0
                elif isinstance(ts, (int, float)) and ts > 1e12:
                    ts = ts / 1000
                pl = e.get("payload") or {}
                t = e.get("type")
                if t == "session_meta":
                    rec["cwd"] = pl.get("cwd") or rec["cwd"]
                    rec["start"] = rec["start"] or ts
                elif t == "response_item" and pl.get("type") == "message":
                    txt = " ".join(c.get("text", "") for c in (pl.get("content") or [])
                                   if isinstance(c, dict)).strip()
                    if pl.get("role") == "user" and not rec["task"] and not txt.startswith(
                            ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions")):
                        rec["task"] = txt[:400]
                    elif pl.get("role") == "assistant" and txt:
                        rec["last_text"] = txt[:400]
                elif t == "event_msg" and pl.get("type") == "token_count":
                    tot = (pl.get("info") or {}).get("total_token_usage") or {}
                    cur = {k: int(tot.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
                    d = dict(cur) if prev is None else {k: max(0, cur[k] - prev.get(k, 0)) for k in cur}
                    prev = cur
                    uncached = max(0, d["input_tokens"] - d["cached_input_tokens"])
                    rec["tin"] += uncached; rec["tout"] += d["output_tokens"]; rec["tcache"] += d["cached_input_tokens"]
                    rec["usd"] += uncached * 0.14e-6 + d["output_tokens"] * 4e-6 + d["cached_input_tokens"] * 0.014e-6
                    rec["end"] = max(rec["end"], ts or 0)
                    if ts:
                        times.append(ts)
                elif t == "event_msg" and pl.get("type") == "task_started":
                    rec["start"] = rec["start"] or ts
        if not rec["start"]:
            continue
        key = os.path.basename(path).replace(".jsonl", "")[-12:]
        label = (rec["task"] or "codex session")[:120]
        out.append({
            "worker": "Codex", "kind": "codex", "key": key, "path": path,
            "state": "done" if rec["end"] else "RUNNING",
            "tokens": rec["tin"] + rec["tout"] + rec["tcache"],
            "tok_in": rec["tin"], "tok_out": rec["tout"], "tok_cache": rec["tcache"],
            "cost_usd": round(rec["usd"], 6),
            "cost_cny": cost_cny_of(rec["tin"], rec["tout"], rec["tcache"], rec["start"]),
            "segs": activity_segments(times, running=not rec["end"]),
            "title": label, "task": rec["task"], "label": label,
            "project": os.path.basename((rec["cwd"] or "?").rstrip("/")) or "?",
            "start": rec["start"], "end": rec["end"], "turns": 0, "steps": 0,
            "tools": {}, "tool_calls": [], "last_text": rec["last_text"],
            "created": rec["start"], "last": rec["end"] or rec["start"],
            "last_start": rec["end"] or rec["start"],
        })
    return out


ALL_WORKERS = ["dsh", "pi", "hermes", "codex"]


def collect(workers=None):
    """汇总所有来源。各来源按自己的会话 id 独立记录，天然不重叠：
    DSH 会话 / Pi 会话 / Pi RPC / Hermes 主循环 / Hermes 子 agent / Codex rollout。"""
    workers = workers or ALL_WORKERS
    recs = []
    if "dsh" in workers:
        recs += scan_dsh()
    if "pi" in workers:
        recs += scan_pi_sessions() + scan_pi_rpc()
    if "hermes" in workers:
        recs += scan_hermes() + scan_hermes_subagents()
    if "codex" in workers:
        recs += scan_codex()
    seen, uniq = set(), []
    for r in recs:
        k = (r.get("kind"), r.get("key"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: r["start"], reverse=True)
    return uniq


def _dur(rec):
    end = rec.get("end") or time.time()
    return max(0.0, end - (rec.get("start") or end))


def print_table(recs, limit, show_cost=True):
    hdr = f"{'TIME':<15} {'DUR':>7}  {'WORKER':<7} {'PROJECT':<20} {'STATE':<11} {'TOKENS':>7} {'≈¥':>8}  {'KEY':<9} TASK"
    print(hdr)
    print("-" * len(hdr))
    for r in recs[:limit]:
        cost = f"≈¥{r['cost_cny']:.2f}" if r.get("cost_cny") else "-"
        print(
            f"{_fmt_time(r['start']):<15} {_fmt_dur(_dur(r)):>7}  {r['worker']:<7} "
            f"{(r.get('project') or '-')[:20]:<20} {r['state']:<11} {_fmt_tok(r.get('tokens')):>7} "
            f"{cost:>8}  {r['key']:<9} {_one_line(r.get('label'), 60)}"
        )
    if show_cost:
        print("\n≈¥ = 六种执行者统一按 deepseek-flash 官方价估算（空闲：缓存 ¥0.02 / 输入 ¥1 / 输出 ¥4 每百万 token；北京工作日 9-12、14-18 双倍）。")


def show(rec):
    print(f"KEY      {rec['key']}   [{rec['worker']}]  状态 {rec['state']}")
    print(f"项目     {rec.get('cwd') or rec.get('project')}")
    print(f"开始     {_fmt_time(rec['start'])}   耗时 {_fmt_dur(_dur(rec))}")
    print(f"tokens   {rec.get('tokens')}   ≈¥{(rec.get('cost_cny') or 0):.3f}"
          f"（上游自报 ${(rec.get('cost_usd') or 0):.4f}）")
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
