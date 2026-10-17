#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_flow.py — agent/harness 工作流面板（采集 + 历史 + 本地 HTTP 服务）。

看什么
======
  节点 = 参与者（Neil → Hermes 主循环 → 执行者 → 工作区）
  边   = 一次派活；同两点之间可有多条（每一步一条，曲率错开）
  忙闲 = 活跃会话数（离散）+ 近 5 分钟 token 速率（强度）
  阶段 = 最近工具名推断：侦察 / 实现 / 自检 / 收尾
  钱   = 按 deepseek-flash 官方价（空闲 ¥1/¥4，高峰双倍）从 usage 算
  历史 = 每个任务落盘到 ~/.hermes/state/agent_flow/tasks.json，支持 24h 回放

数据来源（全部只读）
==================
  DSH 会话    ~/.dsh/sessions/<cwd-slug>/session-<uuid>/session.v*.jsonl.zstd
  Pi 一次性   ~/.pi/agent/sessions/<cwd-slug>/*.jsonl
  Pi RPC      ~/.hermes/logs/pi-rpc-<name>.jsonl
  Codex       ~/.codex/sessions/**/*.jsonl
  子 agent    ~/.hermes/state.db  → async_delegations（真实派活账本）
  Hermes 自身 ~/.hermes/logs/agent.log
  派活登记    ~/.hermes/state/agent_flow/dispatches/*.json（af_dispatch.py 写入）

用法
====
  ~/.hermes/scripts/agent_flow.py                # 起服务（127.0.0.1:8799）
  ~/.hermes/scripts/agent_flow.py --dump         # 打印一次实时快照 JSON
  ~/.hermes/scripts/agent_flow.py --history 24   # 打印 24h 历史任务表（调试）
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
DSH_SESSIONS = os.path.join(HOME, ".dsh", "sessions")
PI_SESSIONS = os.path.join(HOME, ".pi", "agent", "sessions")
PI_RPC = os.path.join(HOME, ".hermes", "logs")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
STATE_DB = os.path.join(HOME, ".hermes", "state.db")
AGENT_LOG = os.path.join(HOME, ".hermes", "logs", "agent.log")
FLOW_DIR = os.path.join(HOME, ".hermes", "state", "agent_flow")
HIST_FILE = os.path.join(FLOW_DIR, "tasks.json")
DISPATCH_DIR = os.path.join(FLOW_DIR, "dispatches")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "agent_flow.html")
TZ = timezone(timedelta(hours=8))

READ_ONLY = {"read", "grep", "find", "ls", "glob", "list", "view", "search"}
EDIT = {"write", "edit", "patch", "apply_patch", "multiedit", "notebookedit", "create"}
TEST_HINT = re.compile(r"\b(pytest|cargo (test|check)|npm (run )?test|pnpm (run )?test|vitest|jest|go test|make test|unittest|selftest)\b", re.I)

# deepseek-flash 官方价（元 / 百万 token）；高峰 = 北京工作日 9-12、14-18
PRICE = {"in": 1.0, "out": 4.0, "cache": 0.02}


def now_ts():
    return time.time()


def peak(ts):
    d = datetime.fromtimestamp(ts, TZ)
    if d.weekday() >= 5:
        return False
    h = d.hour + d.minute / 60
    return (9 <= h < 12) or (14 <= h < 18)


def cost_cny(ts, in_tok=0, out_tok=0, cache_tok=0):
    return (2 if peak(ts) else 1) * (
        in_tok * PRICE["in"] + out_tok * PRICE["out"] + cache_tok * PRICE["cache"]) / 1e6


def jloads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def iter_zstd(path):
    try:
        with subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as p:
            for raw in p.stdout:
                raw = raw.strip()
                if raw:
                    e = jloads(raw)
                    if e is not None:
                        yield e
    except FileNotFoundError:
        return


def ms_of(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def one_line(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n] + "…" if len(s) > n else s


def classify_stage(names, args_recent):
    if not names:
        return "思考"
    edit = sum(1 for n in names if n in EDIT)
    tests = sum(1 for a in args_recent if TEST_HINT.search(str(a)))
    ro = sum(1 for n in names if n in READ_ONLY)
    if tests:
        return "自检"
    if edit:
        return "实现"
    if ro:
        return "侦察"
    return "执行"


# ------------------------------------------------------------------ 进程 / 登记

def list_procs():
    try:
        out = subprocess.run(["ps", "-eo", "pid,ppid,command"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if m:
            rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "cmd": m.group(3)})
    return rows


def load_dispatches():
    """af_dispatch.py 登记过的派活（真实父子来源）。"""
    regs = []
    if not os.path.isdir(DISPATCH_DIR):
        return regs
    for p in glob.glob(os.path.join(DISPATCH_DIR, "*.json")):
        e = jloads(open(p, encoding="utf-8", errors="replace").read())
        if isinstance(e, dict):
            e["_file"] = p
            regs.append(e)
    regs.sort(key=lambda r: r.get("ts", 0))
    return regs


def worker_procs(procs):
    """活着的执行者进程 → {kind: [pid]}"""
    found = {}
    for p in procs:
        c = p["cmd"]
        kind = None
        if "bin.ts headless" in c or "dsh headless" in c:
            kind = "dsh"
        elif "pi-coding-agent" in c or re.search(r"/pi\s+-p\b|/pi --mode", c):
            kind = "pi"
        elif re.search(r"\bcodex exec\b", c):
            kind = "codex"
        if kind:
            found.setdefault(kind, []).append(p["pid"])
    return found


def parent_evidence(task_kind, task_start, regs, live_procs):
    """登记优先 → 进程观察 → 推断；返回 (parent, evidence)"""
    for r in reversed(regs):
        if r.get("kind") == task_kind and abs((r.get("ts") or 0) - (task_start or 0)) < 6 * 3600:
            return r.get("parent") or "hermes", "registry"
    if live_procs.get(task_kind):
        return "hermes", "process"
    return "hermes", "inferred"


# ------------------------------------------------------------------------ DSH

def scan_dsh(window_s, max_age_s=3 * 3600):
    now = now_ts()
    out = []
    for sd in glob.glob(os.path.join(DSH_SESSIONS, "*", "session-*")):
        files = glob.glob(os.path.join(sd, "session.v*.jsonl.zstd"))
        if not files:
            continue
        path = max(files, key=lambda p: int((re.search(r"v(\d+)", p) or [0, 0])[1] or 0))
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > max_age_s:
            continue
        rec = {"kind": "dsh", "path": path, "key": os.path.basename(sd).replace("session-", "")[:8],
               "cwd": "", "title": "", "task": "", "start": 0.0, "last_event": mtime,
               "tokens": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0,
               "tokens_recent": 0, "cost_recent": 0.0, "tool_recent": [], "arg_recent": [],
               "last_text": "", "reason": None, "steps": 0, "samples": []}
        for e in iter_zstd(path):
            t = e.get("type")
            d = e.get("data") or {}
            ts = ms_of(e.get("time")) or mtime
            rec["last_event"] = max(rec["last_event"], ts)
            if t == "session":
                rec["cwd"] = e.get("cwd") or d.get("cwd") or ""
                rec["start"] = ms_of(e.get("createdAt") or d.get("createdAt")) or ts
            elif t == "session/title":
                rec["title"] = d.get("title") or rec["title"]
            elif t == "user/message" and not rec["task"]:
                for c in d.get("content") or []:
                    if c.get("type") == "text":
                        rec["task"] = c.get("text") or ""
                        break
            elif t == "step/start":
                rec["steps"] += 1
            elif t == "assistant/message":
                u = d.get("usage") or (d.get("message") or {}).get("usage") or {}
                it, ot, ct = int(u.get("inputTokens") or 0), int(u.get("outputTokens") or 0), int(u.get("cacheReadTokens") or 0)
                c0 = cost_cny(ts, it, ot, ct)
                rec["in"] += it; rec["out"] += ot; rec["cache"] += ct
                rec["tokens"] += int(u.get("totalTokens") or 0)
                rec["cost"] += c0
                if now - ts <= 300:
                    rec["tokens_recent"] += int(u.get("totalTokens") or 0)
                    rec["cost_recent"] += c0
                txt = " ".join(c.get("text", "") for c in (d.get("message") or {}).get("content") or []
                               if c.get("type") == "text")
                if txt.strip():
                    rec["last_text"] = txt
            elif t == "tool/call":
                if now - ts <= window_s:
                    rec["tool_recent"].append((ts, d.get("name") or "?"))
                    rec["arg_recent"].append(str(d.get("arguments"))[:400])
            elif t == "turn/end":
                rec["reason"] = (d.get("reason") or {}).get("kind")
        rec["project"] = os.path.basename(rec["cwd"]) or "?"
        rec["running"] = (now - rec["last_event"] < 120) and rec["reason"] is None
        rec["waiting"] = rec["reason"] == "completed" and (now - rec["last_event"] <= window_s)
        rec["stage"] = classify_stage([n for _, n in rec["tool_recent"]], rec["arg_recent"])
        rec["last_event_ago"] = now - rec["last_event"]
        rec["end"] = rec["last_event"]
        out.append(rec)
    out.sort(key=lambda r: r["last_event"], reverse=True)
    return out


# ------------------------------------------------------------------------- Pi

def scan_pi(window_s, max_age_s=3 * 3600):
    now = now_ts()
    out = []
    for path in glob.glob(os.path.join(PI_RPC, "pi-rpc-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > max_age_s:
            continue
        name = re.sub(r"^pi-rpc-|\.jsonl$", "", os.path.basename(path))
        rec = {"kind": "pi-rpc", "path": path, "key": name, "cwd": "", "project": "pi-rpc",
               "title": name, "task": "", "start": 0.0, "last_event": 0.0, "reason": None,
               "tokens": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0,
               "tokens_recent": 0, "cost_recent": 0.0, "tool_recent": [], "arg_recent": [],
               "last_text": "", "steps": 0}
        settled = False
        try:
            lines = open(path, encoding="utf-8", errors="replace").readlines()[-800:]
        except OSError:
            continue
        for line in lines:
            e = jloads(line)
            if not e:
                continue
            t = e.get("type")
            msg = e.get("message") or {}
            ts = ms_of(msg.get("timestamp")) or ms_of(e.get("timestamp")) or mtime
            rec["last_event"] = max(rec["last_event"], ts)
            if t == "turn_end" and msg.get("role") == "assistant":
                u = msg.get("usage") or {}
                it, ot, ct = int(u.get("input") or 0), int(u.get("output") or 0), int(u.get("cacheRead") or 0)
                c0 = cost_cny(ts, it, ot, ct)
                rec["in"] += it; rec["out"] += ot; rec["cache"] += ct
                rec["tokens"] += int(u.get("totalTokens") or 0)
                rec["cost"] += c0
                if now - ts <= 300:
                    rec["tokens_recent"] += int(u.get("totalTokens") or 0)
                    rec["cost_recent"] += c0
                txt = " ".join(c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text")
                if txt.strip():
                    rec["last_text"] = txt
            elif t == "tool_execution_start":
                if now - ts <= window_s:
                    rec["tool_recent"].append((ts, e.get("toolName") or e.get("name") or "?"))
                    rec["arg_recent"].append(json.dumps(e.get("args") or e.get("input"))[:400])
            elif t == "agent_settled":
                settled = True
            elif t == "agent_start":
                settled = False
                rec["start"] = rec["start"] or ts
        rec["start"] = rec["start"] or rec["last_event"]
        rec["running"] = (now - rec["last_event"] < 90) and not settled
        rec["waiting"] = settled and (now - rec["last_event"] <= window_s)
        rec["stage"] = classify_stage([n for _, n in rec["tool_recent"]], rec["arg_recent"])
        rec["last_event_ago"] = now - rec["last_event"]
        rec["end"] = rec["last_event"]
        out.append(rec)

    for path in glob.glob(os.path.join(PI_SESSIONS, "*", "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > max_age_s:
            continue
        rec = {"kind": "pi", "path": path, "key": os.path.basename(path).split("_")[-1][:8],
               "cwd": "", "project": "?", "title": "", "task": "", "start": 0.0,
               "last_event": 0.0, "reason": None, "tokens": 0, "in": 0, "out": 0, "cache": 0,
               "cost": 0.0, "tokens_recent": 0, "cost_recent": 0.0, "tool_recent": [],
               "arg_recent": [], "last_text": "", "steps": 0}
        ended = False
        try:
            lines = open(path, encoding="utf-8", errors="replace").readlines()
        except OSError:
            continue
        for line in lines:
            e = jloads(line)
            if not e:
                continue
            t = e.get("type")
            if t == "session":
                rec["cwd"] = e.get("cwd") or ""
                rec["start"] = ms_of(e.get("timestamp"))
            elif t == "session_info":
                rec["title"] = e.get("name") or rec["title"]
            elif t == "message":
                msg = e.get("message") or {}
                ts = ms_of(e.get("timestamp")) or mtime
                rec["last_event"] = max(rec["last_event"], ts)
                role = msg.get("role")
                if role == "user" and not rec["task"]:
                    c = msg.get("content")
                    rec["task"] = c if isinstance(c, str) else " ".join(
                        x.get("text", "") for x in c or [] if isinstance(x, dict))
                elif role == "assistant":
                    u = msg.get("usage") or {}
                    it, ot, ct = int(u.get("input") or 0), int(u.get("output") or 0), int(u.get("cacheRead") or 0)
                    c0 = cost_cny(ts, it, ot, ct)
                    rec["in"] += it; rec["out"] += ot; rec["cache"] += ct
                    rec["tokens"] += int(u.get("totalTokens") or 0)
                    rec["cost"] += c0
                    if now - ts <= 300:
                        rec["tokens_recent"] += int(u.get("totalTokens") or 0)
                        rec["cost_recent"] += c0
                    if (msg.get("stopReason") or "") == "stop":
                        ended = True
                    for c in msg.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "toolCall" and now - ts <= window_s:
                            rec["tool_recent"].append((ts, c.get("name") or "?"))
                            rec["arg_recent"].append(json.dumps(c.get("arguments"))[:400])
        rec["project"] = os.path.basename(rec["cwd"]) or "?"
        rec["running"] = (now - rec["last_event"] < 90) and not ended
        rec["waiting"] = ended and (now - rec["last_event"] <= window_s)
        rec["stage"] = classify_stage([n for _, n in rec["tool_recent"]], rec["arg_recent"])
        rec["last_event_ago"] = now - rec["last_event"]
        rec["end"] = rec["last_event"]
        out.append(rec)
    return out


# ------------------------------------------------------- Codex / 子 agent / 主循环

def read_codex(path):
    """解析 codex rollout：cwd / 任务 / 每次 token_count 的增量事件（含累计成本）。"""
    now = now_ts()
    rec = {"cwd": "", "task": "", "start": 0.0, "end": 0.0, "events": [], "last_text": "",
           "in": 0, "out": 0, "cache": 0}
    prev = None
    cum_cost = 0.0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return rec
    with fh:
        for line in fh:
            e = jloads(line)
            if not e:
                continue
            ts = ms_of(e.get("timestamp"))
            t = e.get("type")
            pl = e.get("payload") or {}
            if t == "session_meta":
                rec["cwd"] = pl.get("cwd") or rec["cwd"]
                rec["start"] = rec["start"] or ms_of(pl.get("timestamp")) or ts
            elif t == "turn_context":
                rec["cwd"] = rec["cwd"] or pl.get("cwd") or ""
                rec["start"] = rec["start"] or ts
            elif t == "response_item" and pl.get("type") == "message":
                role = pl.get("role")
                txt = " ".join(c.get("text", "") for c in pl.get("content") or []
                               if isinstance(c, dict) and c.get("type") in ("input_text", "text", "output_text")).strip()
                if role == "user" and not rec["task"]:
                    # 跳过环境上下文/权限说明这类系统注入，只认真正的用户任务
                    if not txt.startswith(("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions")):
                        rec["task"] = one_line(txt, 400)
                elif role == "assistant" and txt:
                    rec["last_text"] = txt
            elif t == "event_msg":
                pt = pl.get("type")
                if pt == "task_started":
                    rec["start"] = rec["start"] or ts
                elif pt == "token_count":
                    tot = (pl.get("info") or {}).get("total_token_usage") or {}
                    cur = {k: int(tot.get(k) or 0) for k in
                           ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")}
                    if prev is None:
                        delta = dict(cur)
                    else:
                        delta = {k: max(0, cur[k] - prev.get(k, 0)) for k in cur}
                    prev = cur
                    uncached = max(0, delta["input_tokens"] - delta["cached_input_tokens"])
                    d_cost = cost_cny(ts or now, uncached, delta["output_tokens"], delta["cached_input_tokens"])
                    cum_cost += d_cost
                    rec["in"] += delta["input_tokens"]
                    rec["out"] += delta["output_tokens"]
                    rec["cache"] += delta["cached_input_tokens"]
                    rec["events"].append((ts or now, cur["total_tokens"], cum_cost, delta["total_tokens"], d_cost))
                    rec["end"] = max(rec["end"], ts or now)
    if not rec["start"]:
        rec["start"] = rec["end"]
    rec["cost"] = cum_cost
    rec["tokens"] = rec["events"][-1][1] if rec["events"] else 0
    return rec


def scan_codex(window_s):
    now = now_ts()
    out = []
    for path in glob.glob(os.path.join(CODEX_SESSIONS, "**", "*.jsonl"), recursive=True):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > 3 * 3600:
            continue
        r = read_codex(path)
        recent = [ev for ev in r["events"] if now - ev[0] <= 300]
        tools = []
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                e = jloads(line)
                if not e:
                    continue
                pl = e.get("payload") or {}
                if e.get("type") == "response_item" and pl.get("type") in ("function_call", "custom_tool_call"):
                    ts = ms_of(e.get("timestamp"))
                    if ts and now - ts <= window_s:
                        tools.append((ts, pl.get("name") or "?"))
        except OSError:
            pass
        out.append({"kind": "codex", "path": path, "key": os.path.basename(path)[-16:-8],
                    "cwd": r["cwd"], "project": os.path.basename(r["cwd"]) or "?", "title": "codex",
                    "task": r["task"], "start": r["start"], "last_event": r["end"] or mtime, "reason": None,
                    "tokens": r["tokens"], "in": 0, "out": 0, "cache": 0,
                    "cost": r["cost"], "tokens_recent": sum(ev[3] for ev in recent),
                    "cost_recent": sum(ev[4] for ev in recent),
                    "tool_recent": tools, "arg_recent": [], "last_text": r["last_text"], "steps": 0,
                    "running": now - (r["end"] or mtime) < 120 and not r["last_text"],
                    "waiting": False, "stage": classify_stage([n for _, n in tools], []),
                    "last_event_ago": now - (r["end"] or mtime), "end": r["end"] or mtime})
    return out


def scan_subagents(window_s):
    now = now_ts()
    out = []
    if not os.path.exists(STATE_DB):
        return out
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute(
            "select delegation_id, origin_session, state, dispatched_at, completed_at, task_json, "
            "result_json, event_json from async_delegations order by dispatched_at desc limit 60").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for did, origin, state, disp, comp, task_json, result_json, event_json in rows:
        if now - (disp or 0) > max(window_s, 6 * 3600):
            continue
        goal, summary, tokens, cost = "", "", 0, 0.0
        try:
            tj = json.loads(task_json or "{}")
            tasks = tj.get("tasks") or []
            goal = (tasks[0].get("goal") or "") if tasks else ""
        except Exception:
            pass
        try:
            ej = json.loads(event_json or "{}")
            goal = goal or (ej.get("goal") or "")
        except Exception:
            pass
        try:
            rj = json.loads(result_json or "{}")
            for item in rj.get("results") or []:
                tk = item.get("tokens") or {}
                it = int(tk.get("input") or 0)
                ot = int(tk.get("output") or 0)
                tokens += it + ot + int(tk.get("cache_read") or 0)
                if it or ot:
                    cost += cost_cny(comp or disp or now, it, ot, int(tk.get("cache_read") or 0))
                else:
                    cost += float(item.get("cost_usd") or 0) * 7.1   # 没有 token 明细时用 Hermes 自报美元折算
                if item.get("summary"):
                    summary = one_line(str(item["summary"]), 300)
        except Exception:
            pass
        out.append({"kind": "subagent", "path": "", "key": did.replace("deleg_", ""),
                    "cwd": "", "project": "子 agent", "title": "delegate", "task": one_line(goal, 400),
                    "start": disp or 0, "last_event": comp or disp or 0, "reason": None,
                    "tokens": tokens, "in": 0, "out": 0, "cache": 0, "cost": cost,
                    "tokens_recent": 0, "cost_recent": 0.0, "tool_recent": [], "arg_recent": [],
                    "last_text": summary, "steps": 0,
                    "running": state in ("running", "pending", "dispatched"),
                    "waiting": state in ("completed", "failed"), "stage": "执行",
                    "last_event_ago": now - (comp or disp or now), "end": comp or disp or 0,
                    "origin": origin, "state": state})
    return out


def scan_hermes():
    info = {"last_api": 0.0, "last_tool": 0.0, "last_cron": 0.0}
    if not os.path.exists(AGENT_LOG):
        return info
    try:
        with open(AGENT_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 300_000))
            tail = f.read().decode("utf-8", "replace").splitlines()[-4000:]
    except OSError:
        return info
    for line in tail:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue
        if "agent.conversation_loop: API call" in line:
            info["last_api"] = max(info["last_api"], ts)
        elif "tool_executor: tool " in line:
            info["last_tool"] = max(info["last_tool"], ts)
        elif "cron.scheduler: Job" in line:
            info["last_cron"] = max(info["last_cron"], ts)
    return info


# --------------------------------------------------------------------- 历史存储

def load_history():
    if not os.path.exists(HIST_FILE):
        return {"version": 1, "tasks": {}}
    try:
        h = json.load(open(HIST_FILE, encoding="utf-8"))
        if isinstance(h, dict) and "tasks" in h:
            return h
    except Exception:
        pass
    return {"version": 1, "tasks": {}}


def save_history(h):
    os.makedirs(FLOW_DIR, exist_ok=True)
    tmp = HIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)
    os.replace(tmp, HIST_FILE)


def upsert_history(h, tasks, now):
    """把实时任务合并进历史；每 15s 采一个点，7 天保留。"""
    changed = False
    for t in tasks:
        k = f"{t['kind']}:{t['key']}"
        r = h["tasks"].get(k) or {"key": t["key"], "kind": t["kind"], "samples": []}
        state = "active" if t["running"] else ("waiting" if t.get("waiting") else "done")
        before = (r.get("state"), r.get("tokens"), r.get("stage"))
        r.update({
            "label": one_line(t.get("task") or t.get("title") or t["key"], 200),
            "full": t.get("task") or t.get("title") or "",
            "project": t.get("project") or "",
            "start": r.get("start") or t.get("start") or t["last_event"],
            "end": None if state == "active" else t["last_event"],
            "state": state, "tokens": t["tokens"], "cost": round(t.get("cost") or 0, 6),
            "in": t.get("in") or 0, "out": t.get("out") or 0, "cache": t.get("cache") or 0,
            "stage": t["stage"], "log": t["path"], "parent": t.get("parent", "hermes"),
            "evidence": t.get("evidence", "inferred"),
        })
        if not r.get("first_seen"):
            r["first_seen"] = now
        samples = r["samples"]
        if not samples or now - samples[-1][0] >= 15 or t["running"] is False:
            samples.append([round(now, 1), int(t["tokens"]), round(t.get("cost") or 0, 5), t["stage"],
                            "active" if t["running"] else state])
            if len(samples) > 6000:
                del samples[:len(samples) - 6000]
        if (r.get("state"), r.get("tokens"), r.get("stage")) != before:
            changed = True
        h["tasks"][k] = r
    # 保留 7 天
    cut = now - 7 * 86400
    for k in [k for k, r in h["tasks"].items() if (r.get("end") or r.get("start") or 0) < cut]:
        del h["tasks"][k]
        changed = True
    return changed


# ------------------------------------------------------------------------ 组图

NODE_DEFS = [
    ("neil", "Neil", "human", 0, 46),
    ("hermes", "Hermes", "orchestrator", 1, 58),
    ("dsh", "DSH headless", "worker", 2, 48),
    ("pi", "Pi", "worker", 2, 40),
    ("pi-rpc", "Pi RPC", "worker", 2, 38),
    ("codex", "Codex CLI", "worker", 2, 36),
    ("subagent", "Hermes Subagent", "worker", 2, 38),
]
KIND2NODE = {"dsh": "dsh", "pi": "pi", "pi-rpc": "pi-rpc", "codex": "codex", "subagent": "subagent"}


def build_state(window_min=15, history=None):
    now = now_ts()
    window_s = window_min * 60
    procs = list_procs()
    live = worker_procs(procs)
    regs = load_dispatches()

    tasks = scan_dsh(window_s) + scan_pi(window_s) + scan_codex(window_s) + scan_subagents(window_s)
    for t in tasks:
        parent, ev = parent_evidence(t["kind"], t.get("start"), regs, live)
        t["parent"], t["evidence"] = parent, ev
    h = scan_hermes()
    hermes_busy = (now - h["last_api"] < 90) or (now - h["last_tool"] < 90)

    recent = [t for t in tasks if t["last_event_ago"] <= window_s]
    active = [t for t in tasks if t["running"]]

    hist = history if history is not None else load_history()
    if upsert_history(hist, tasks, now):
        save_history(hist)

    def agg(kind):
        ts = [t for t in tasks if t["kind"] == kind and t["last_event_ago"] <= window_s]
        return {
            "sessions": len([t for t in ts if t["running"]]),
            "tokens_recent": sum(t["tokens_recent"] for t in ts),
            "tokens_window": sum(t["tokens"] for t in ts),
            "cost_recent": sum(t["cost_recent"] for t in ts),
            "cost_window": sum(t["cost"] for t in ts),
            "stage": next((t["stage"] for t in ts if t["running"]), None),
        }

    aggs = {k: agg(k) for k in ("dsh", "pi", "pi-rpc", "codex", "subagent")}
    today0 = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    cost_today = sum(r.get("cost") or 0 for r in hist["tasks"].values()
                     if (r.get("start") or 0) >= today0)

    projects = {}
    for t in recent:
        p = t.get("project")
        if p and p not in ("?", "子 agent", "pi-rpc"):
            d = projects.setdefault(p, {"tasks": 0, "running": 0, "cost": 0.0, "kinds": set()})
            d["tasks"] += 1
            d["running"] += 1 if t["running"] else 0
            d["cost"] += t.get("cost") or 0
            d["kinds"].add(t["kind"])

    nodes = []
    for nid, label, role, layer, base in NODE_DEFS:
        a = aggs.get(nid) if nid in aggs else None
        if nid == "hermes":
            busy = 1.0 if hermes_busy else 0.15
            nodes.append({"id": nid, "label": label, "role": role, "layer": layer, "base": base,
                          "busy": busy, "sessions": 1 if hermes_busy else 0, "tokens_recent": 0,
                          "cost_recent": 0.0, "cost_window": 0.0, "stage": "编排" if hermes_busy else None,
                          "note": "调度 / 验收"})
        elif nid == "neil":
            nodes.append({"id": nid, "label": label, "role": role, "layer": layer, "base": base,
                          "busy": 1.0 if active else 0.1, "sessions": 1 if active else 0,
                          "tokens_recent": 0, "cost_recent": 0.0, "cost_window": 0.0,
                          "stage": None, "note": "指令源"})
        else:
            busy = min(1.0, a["sessions"] * 0.55 + a["tokens_recent"] / 250_000)
            nodes.append({"id": nid, "label": label, "role": role, "layer": layer, "base": base,
                          "busy": busy, "sessions": a["sessions"], "tokens_recent": a["tokens_recent"],
                          "tokens_window": a["tokens_window"],
                          "cost_recent": a["cost_recent"], "cost_window": a["cost_window"],
                          "stage": a["stage"], "note": {
                              "dsh": "沙箱 · 长链", "pi": "YOLO · 单点", "pi-rpc": "常驻 · 可 steer",
                              "codex": "按需", "subagent": "delegate_task"}[nid]})
    for name, p in sorted(projects.items(), key=lambda kv: -kv[1]["running"]):
        nodes.append({"id": "proj:" + name, "label": name, "role": "project", "layer": 3, "base": 30,
                      "busy": min(1.0, p["running"] * 0.5 + p["tasks"] * 0.1), "sessions": p["running"],
                      "tokens_recent": 0, "cost_recent": 0.0, "cost_window": p["cost"],
                      "stage": None, "note": f"{p['tasks']} 个任务在窗内",
                      "kinds": sorted(p["kinds"])})

    edges = [{"id": "e:neil-hermes", "from": "neil", "to": "hermes", "kind": "request",
              "label": "指令", "state": "active" if active else "recent",
              "started_at": active[0]["start"] if active else 0, "ended_at": now,
              "idx": 0, "cost": 0.0, "tokens": 0, "evidence": "n/a"}]
    per_pair = {}
    for t in sorted(recent, key=lambda r: r["last_event"]):
        nid = KIND2NODE.get(t["kind"])
        if not nid:
            continue
        per_pair[nid] = per_pair.get(nid, 0) + 1
        raw = t.get("task") or t.get("title") or t["key"]
        clean = re.sub(r"\s+", " ", re.sub(r"[`#>*_]+", "", re.sub(r"^\s*[#>\-\*]+\s*", "", raw or ""))).strip()
        state = "active" if t["running"] else ("waiting" if t.get("waiting") else "recent")
        edges.append({"id": f"e:hermes-{nid}:{t['kind']}:{t['key']}", "from": "hermes", "to": nid,
                      "kind": "dispatch", "label": one_line(clean, 46), "full": raw,
                      "state": state, "started_at": t["start"], "ended_at": t["last_event"],
                      "idx": per_pair[nid] - 1, "tokens": t["tokens"], "tokens_recent": t["tokens_recent"],
                      "cost": round(t.get("cost") or 0, 4), "cost_recent": round(t.get("cost_recent") or 0, 4),
                      "stage": t["stage"], "key": t["key"], "log": t["path"], "project": t.get("project"),
                      "evidence": t.get("evidence", "inferred"), "parent": t.get("parent")})
        if state != "recent" and t.get("project") and t["project"] not in ("?", "子 agent", "pi-rpc"):
            edges.append({"id": f"e:{nid}-proj:{t['key']}", "from": nid, "to": "proj:" + t["project"],
                          "kind": "workspace", "label": "", "state": state, "started_at": t["start"],
                          "ended_at": t["last_event"], "idx": 0, "tokens": 0, "cost": 0.0,
                          "key": t["key"], "log": t["path"], "project": t.get("project"),
                          "evidence": t.get("evidence", "inferred")})

    timeline = [{"key": t["key"], "kind": t["kind"], "project": t.get("project") or "",
                 "label": one_line(t.get("task") or t.get("title") or t["key"], 80),
                 "start": t["start"] or t["last_event"], "end": t["last_event"],
                 "state": "active" if t["running"] else ("waiting" if t.get("waiting") else "recent"),
                 "stage": t["stage"], "tokens": t["tokens"], "cost": round(t.get("cost") or 0, 4),
                 "log": t["path"]} for t in sorted(recent, key=lambda r: r["start"])]

    return {"generated_at": now, "window_min": window_min, "live": True,
            "counts": {"active_tasks": len(active), "tokens_5m": sum(t["tokens_recent"] for t in tasks),
                       "cost_today": round(cost_today, 3),
                       "cost_window": round(sum(t.get("cost") or 0 for t in recent), 3)},
            "nodes": nodes, "edges": edges, "timeline": timeline,
            "history_span": {"tasks": len(hist["tasks"]), "from": min(
                [(r.get("start") or 0) for r in hist["tasks"].values()] or [now])}}


def replay_state(t, history=None):
    """按历史记录重建 t 时刻的图（24h 回放）。"""
    hist = history if history is not None else load_history()
    nodes = [{"id": nid, "label": label, "role": role, "layer": layer, "base": base,
              "busy": 0.1, "sessions": 0, "tokens_recent": 0, "cost_recent": 0.0, "cost_window": 0.0,
              "stage": None, "note": ""} for nid, label, role, layer, base in NODE_DEFS]
    by_id = {n["id"]: n for n in nodes}
    edges = []
    per_pair = {}
    span = 45 * 60
    tasks = []
    for r in hist["tasks"].values():
        s, e = r.get("start") or 0, r.get("end") or t
        if s > t or e < t - span:
            continue
        # t 时刻状态
        active = s <= t <= (r.get("end") or t)
        tokens_at, cost_at = r.get("tokens", 0), r.get("cost", 0.0)
        for smp in r.get("samples", []):
            if smp[0] <= t:
                tokens_at, cost_at = smp[1], smp[2]
            else:
                break
        tasks.append((r, active, tokens_at, cost_at))
        nid = KIND2NODE.get(r["kind"])
        if not nid:
            continue
        n = by_id.get(nid)
        if active:
            n["sessions"] += 1
            n["stage"] = r.get("stage")
            n["tokens_recent"] += tokens_at
            n["cost_recent"] += cost_at
        per_pair[nid] = per_pair.get(nid, 0) + 1
        state = "active" if active else "recent"
        edges.append({"id": f"h:{r['kind']}:{r['key']}", "from": "hermes", "to": nid, "kind": "dispatch",
                      "label": one_line(r.get("label"), 46), "full": r.get("full"),
                      "state": state, "started_at": s, "ended_at": e, "idx": per_pair[nid] - 1,
                      "tokens": tokens_at, "cost": round(cost_at, 4), "stage": r.get("stage"),
                      "key": r["key"], "log": r.get("log"), "project": r.get("project"),
                      "evidence": r.get("evidence", "inferred")})
        proj = r.get("project")
        if state != "recent" and proj and proj not in ("?", "子 agent", "pi-rpc"):
            pid = "proj:" + proj
            if pid not in by_id:
                by_id[pid] = {"id": pid, "label": proj, "role": "project", "layer": 3, "base": 30,
                              "busy": 0.1, "sessions": 0, "tokens_recent": 0, "cost_recent": 0.0,
                              "cost_window": 0.0, "stage": None, "note": ""}
                nodes.append(by_id[pid])
            edges.append({"id": f"h:{r['kind']}:{r['key']}:p", "from": nid, "to": pid, "kind": "workspace",
                          "label": "", "state": state, "started_at": s, "ended_at": e, "idx": 0,
                          "tokens": 0, "cost": 0.0, "key": r["key"], "project": proj})
    for n in nodes:
        if n["role"] in ("worker", "project"):
            n["busy"] = min(1.0, n["sessions"] * 0.55 + n["tokens_recent"] / 250_000)
    if any(n["sessions"] for n in nodes if n["role"] == "worker"):
        by_id["hermes"]["busy"], by_id["hermes"]["sessions"], by_id["hermes"]["stage"] = 0.9, 1, "编排"
        by_id["neil"]["busy"], by_id["neil"]["sessions"] = 0.9, 1
        edges.insert(0, {"id": "e:neil-hermes", "from": "neil", "to": "hermes", "kind": "request",
                         "label": "指令", "state": "active", "started_at": t, "ended_at": t, "idx": 0,
                         "cost": 0.0, "tokens": 0})
    return {"generated_at": t, "window_min": round(span / 60), "live": False,
            "counts": {"active_tasks": sum(1 for _, a, _, _ in tasks if a),
                       "tokens_5m": sum(tk for _, a, tk, _ in tasks if a),
                       "cost_today": round(sum(c for _, _, _, c in tasks), 3),
                       "cost_window": round(sum(c for _, _, _, c in tasks), 3)},
            "nodes": nodes, "edges": edges, "timeline": []}


# ------------------------------------------------------------------------ 回填

def backfill(days=7):
    """把最近 N 天的会话日志回填成历史记录（分钟粒度），让回放一上来就有内容。"""
    now = now_ts()
    cut = now - days * 86400
    h = load_history()
    added = 0

    def put(kind, key, meta, buckets, start, end, state, split=None):
        nonlocal added
        if not buckets or not start:
            return
        cum_t = cum_c = 0
        samples = []
        for ts in sorted(buckets):
            tok, cost, names = buckets[ts]
            cum_t += tok; cum_c += cost
            samples.append([ts, cum_t, round(cum_c, 5), classify_stage(names, []),
                            "active" if ts < (end or now) - 60 else state])
        rec = {"key": key, "kind": kind, "samples": samples, "start": start, "end": end,
               "state": state, "tokens": cum_t, "cost": cum_c, "stage": samples[-1][3],
               "first_seen": start, "backfilled": True,
               "in": (split or {}).get("in", 0), "out": (split or {}).get("out", 0),
               "cache": (split or {}).get("cache", 0)}
        rec.update(meta)
        h["tasks"][f"{kind}:{key}"] = rec
        added += 1

    # DSH
    for sd in glob.glob(os.path.join(DSH_SESSIONS, "*", "session-*")):
        files = glob.glob(os.path.join(sd, "session.v*.jsonl.zstd"))
        if not files:
            continue
        path = max(files, key=lambda p: int((re.search(r"v(\d+)", p) or [0, 0])[1] or 0))
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        key = os.path.basename(sd).replace("session-", "")[:8]
        meta = {"label": "", "full": "", "project": "", "log": path,
                "parent": "hermes", "evidence": "inferred"}
        buckets, start, end, state = {}, 0.0, 0.0, "done"
        split = {"in": 0, "out": 0, "cache": 0}
        for e in iter_zstd(path):
            t = e.get("type"); d = e.get("data") or {}
            ts = ms_of(e.get("time")) or 0
            if t == "session":
                start = ms_of(e.get("createdAt")) or ts
                meta["project"] = os.path.basename(e.get("cwd") or "") or ""
            elif t == "session/title":
                meta["label"] = one_line(d.get("title"), 200)
            elif t == "user/message" and not meta["full"]:
                for c in d.get("content") or []:
                    if c.get("type") == "text":
                        meta["full"] = c.get("text") or ""
                        break
                if not meta["label"]:
                    meta["label"] = one_line(meta["full"], 200)
            elif t == "assistant/message":
                u = d.get("usage") or {}
                b = int(ts // 60 * 60)
                cell = buckets.setdefault(b, [0, 0.0, []])
                cell[0] += int(u.get("totalTokens") or 0)
                cell[1] += cost_cny(ts, int(u.get("inputTokens") or 0),
                                    int(u.get("outputTokens") or 0), int(u.get("cacheReadTokens") or 0))
                split["in"] += int(u.get("inputTokens") or 0)
                split["out"] += int(u.get("outputTokens") or 0)
                split["cache"] += int(u.get("cacheReadTokens") or 0)
                end = max(end, ts)
            elif t == "tool/call":
                b = int(ts // 60 * 60)
                buckets.setdefault(b, [0, 0.0, []])[2].append(d.get("name") or "?")
            elif t == "turn/end":
                if d.get("reason"):
                    state = {"completed": "done", "aborted": "aborted"}.get(
                        (d.get("reason") or {}).get("kind"), "done")
        put("dsh", key, meta, buckets, start, end, state, split)

    # Pi RPC
    for path in glob.glob(os.path.join(PI_RPC, "pi-rpc-*.jsonl")):
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        name = re.sub(r"^pi-rpc-|\.jsonl$", "", os.path.basename(path))
        buckets, start, end, state = {}, 0.0, 0.0, "done"
        split = {"in": 0, "out": 0, "cache": 0}
        meta = {"label": name, "full": "", "project": "pi-rpc", "log": path,
                "parent": "hermes", "evidence": "inferred"}
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                e = jloads(line)
                if not e:
                    continue
                msg = e.get("message") or {}
                ts = ms_of(msg.get("timestamp")) or ms_of(e.get("timestamp"))
                if e.get("type") == "turn_end" and msg.get("role") == "assistant" and ts:
                    u = msg.get("usage") or {}
                    b = int(ts // 60 * 60)
                    cell = buckets.setdefault(b, [0, 0.0, []])
                    cell[0] += int(u.get("totalTokens") or 0)
                    cell[1] += cost_cny(ts, int(u.get("input") or 0), int(u.get("output") or 0),
                                        int(u.get("cacheRead") or 0))
                    split["in"] += int(u.get("input") or 0)
                    split["out"] += int(u.get("output") or 0)
                    split["cache"] += int(u.get("cacheRead") or 0)
                    start = start or ts
                    end = max(end, ts)
        except OSError:
            continue
        put("pi-rpc", name, meta, buckets, start, end, state, split)

    # Pi 一次性
    for path in glob.glob(os.path.join(PI_SESSIONS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        key = os.path.basename(path).split("_")[-1][:8]
        buckets, start, end, state = {}, 0.0, 0.0, "done"
        split = {"in": 0, "out": 0, "cache": 0}
        meta = {"label": "", "full": "", "project": "", "log": path,
                "parent": "hermes", "evidence": "inferred"}
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                e = jloads(line)
                if not e:
                    continue
                if e.get("type") == "session":
                    start = ms_of(e.get("timestamp"))
                    meta["project"] = os.path.basename(e.get("cwd") or "") or ""
                elif e.get("type") == "session_info":
                    meta["label"] = e.get("name") or meta["label"]
                elif e.get("type") == "message":
                    msg = e.get("message") or {}
                    ts = ms_of(e.get("timestamp"))
                    if msg.get("role") == "user" and not meta["full"]:
                        c = msg.get("content")
                        meta["full"] = c if isinstance(c, str) else one_line(
                            " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict)), 400)
                        meta["label"] = meta["label"] or one_line(meta["full"], 200)
                    elif msg.get("role") == "assistant" and ts:
                        u = msg.get("usage") or {}
                        b = int(ts // 60 * 60)
                        cell = buckets.setdefault(b, [0, 0.0, []])
                        cell[0] += int(u.get("totalTokens") or 0)
                        cell[1] += cost_cny(ts, int(u.get("input") or 0), int(u.get("output") or 0),
                                            int(u.get("cacheRead") or 0))
                        split["in"] += int(u.get("input") or 0)
                        split["out"] += int(u.get("output") or 0)
                        split["cache"] += int(u.get("cacheRead") or 0)
                        end = max(end, ts)
        except OSError:
            continue
        put("pi", key, meta, buckets, start, end, state, split)

    # Codex（rollout 里的 token_count 是累计值，按增量入桶）
    for path in glob.glob(os.path.join(CODEX_SESSIONS, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        r = read_codex(path)
        if not r["start"] or not r["events"]:
            continue
        key = os.path.basename(path)[-16:-8]
        buckets = {}
        for ts, tot, cum_cost, d_tok, d_cost in r["events"]:
            cell = buckets.setdefault(int(ts // 60 * 60), [0, 0.0, []])
            cell[0] += d_tok
            cell[1] += d_cost
        meta = {"label": one_line(r["task"], 200) or "codex", "full": r["task"],
                "project": os.path.basename(r["cwd"]) or "?", "log": path,
                "parent": "hermes", "evidence": "inferred"}
        put("codex", key, meta, buckets, r["start"], r["end"], "done",
            {"in": r["in"], "out": r["out"], "cache": r["cache"]})

    # 子 agent（数据库里只有起止，没有 token/成本）
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute("select delegation_id, state, dispatched_at, completed_at, task_json, result_json "
                           "from async_delegations where dispatched_at > ?", (cut,)).fetchall()
        con.close()
        for did, st, disp, comp, tj, rj_raw in rows:
            goal, tokens, cost = "", 0, 0.0
            try:
                tasks = (json.loads(tj or "{}").get("tasks") or [])
                goal = (tasks[0].get("goal") or "") if tasks else ""
            except Exception:
                pass
            try:
                for item in (json.loads(rj_raw or "{}").get("results") or []):
                    tk = item.get("tokens") or {}
                    it, ot = int(tk.get("input") or 0), int(tk.get("output") or 0)
                    tokens += it + ot + int(tk.get("cache_read") or 0)
                    cost += cost_cny(comp or disp or now, it, ot, 0) if (it or ot) \
                        else float(item.get("cost_usd") or 0) * 7.1
            except Exception:
                pass
            k = did.replace("deleg_", "")
            h["tasks"][f"subagent:{k}"] = {
                "key": k, "kind": "subagent", "label": one_line(goal, 200), "full": goal,
                "project": "子 agent", "start": disp, "end": comp, "state": "done" if st == "completed" else st,
                "tokens": tokens, "cost": cost, "stage": "执行", "log": "",
                "parent": "hermes", "evidence": "registry", "first_seen": disp, "backfilled": True,
                "samples": [[disp, 0, 0.0, "执行", "active"], [comp or disp, tokens, cost, "执行", "done"]],
            }
            added += 1
    except sqlite3.Error:
        pass

    save_history(h)
    return added


# ------------------------------------------------------------------- 演示数据

def demo_history(days=7):
    """`--demo` 的历史：虚构项目、固定种子，7 天流水（供截图/试用，不读本机数据）。"""
    import random
    rnd = random.Random(20261017)
    now = now_ts()
    day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    base = day0.timestamp()
    projs = ["my-app", "api-service", "docs-site"]
    tasks = {
        "my-app": ["Step 3 任务书（导出器合并）", "修复 CI 里的 flaky 测试", "给解码器补边界用例",
                   "把日志换成结构化输出", "profile 一下慢路径"],
        "api-service": ["加 /v1/batch 接口", "补限流中间件", "写迁移脚本", "排查 502 的根因"],
        "docs-site": ["README 翻成英文", "补 quickstart 截图", "改导航结构"],
    }
    kinds = [("dsh", "dsh"), ("dsh", "dsh"), ("dsh", "dsh"), ("pi-rpc", "pi-rpc"),
             ("pi", "pi"), ("codex", "codex"), ("subagent", "subagent")]
    out = {}
    seq = 0
    for d in range(days - 1, -1, -1):
        day_start = base - d * 86400
        n = 2 if d == 0 else rnd.randint(3, 5)
        for i in range(n):
            kind, kindkey = rnd.choice(kinds)
            proj = rnd.choice(projs)
            label = rnd.choice(tasks[proj])
            # 越近的任务越靠后；今天留 2 条在跑/待验收
            hour = 9 + (i * 2) % 9
            start = day_start + hour * 3600 + rnd.randint(0, 1800)
            dur = rnd.choice([120, 300, 480, 900, 1500, 2400])
            if d == 0 and i >= n - 2:
                start = now - (n - i) * 600
                state = "active" if i == n - 1 else "waiting"
                dur = int(now - start)
            else:
                state = "done"
            tin = rnd.randint(8, 140) * 1000
            tcache = rnd.randint(20, 900) * 10000
            tout = rnd.randint(3, 40) * 1000
            tok = tin + tcache + tout
            cost = cost_cny(start, tin, tout, tcache)
            key = f"{kindkey}-{d}{i}{seq}"
            seq += 1
            out[key] = {
                "key": key, "kind": kindkey, "label": label, "full": label, "project": proj,
                "start": start, "end": 0 if state != "done" else start + dur, "state": state,
                "tokens": tok, "cost": cost, "stage": rnd.choice(["侦察", "实现", "自检", "收尾"]),
                "in": tin, "out": tout, "cache": tcache, "demo": True,
            }
    return {"tasks": out}


def demo_state(window_min=15):
    """`--demo`：不读任何本机数据，返回一份固定场景（所有数字都从同一份任务表推）。"""
    now = now_ts()
    window_s = window_min * 60
    tasks = [
        {"kind": "dsh", "key": "d1", "start": now - 17 * 60, "end": now - 5,
         "tokens": 1_846_000, "cost": 0.42, "tokens_recent": 612_000, "cost_recent": 0.14,
         "stage": "实现", "state": "active", "project": "demo-app",
         "task": "Step 12：缓存预热 + 增量视图（长链任务，沙箱内改 4 个文件）"},
        {"kind": "pi-rpc", "key": "p1", "start": now - 11 * 60, "end": now - 1,
         "tokens": 126_000, "cost": 0.031, "tokens_recent": 54_000, "cost_recent": 0.013,
         "stage": "侦察", "state": "active", "project": "demo-app",
         "task": "只读侦察：定位解码热点，列出候选改动点"},
        {"kind": "codex", "key": "c1", "start": now - 9 * 60, "end": now - 6 * 60,
         "tokens": 231_000, "cost": 0.088, "tokens_recent": 0, "cost_recent": 0.0,
         "stage": "自检", "state": "waiting", "project": "",
         "task": "跑一遍跨平台构建冒烟，贴真实输出"},
        {"kind": "subagent", "key": "s1", "start": now - 12 * 60, "end": now - 8 * 60,
         "tokens": 449_000, "cost": 0.55, "tokens_recent": 0, "cost_recent": 0.0,
         "stage": "执行", "state": "recent", "project": "",
         "task": "并行调研：三个第三方库的 API 差异"},
        # 窗口外的历史任务：只体现在"待命"池的 Pi 上，不进甘特
        {"kind": "pi", "key": "q1", "start": now - (window_min + 11) * 60, "end": now - (window_min + 7) * 60,
         "tokens": 64_000, "cost": 0.016, "tokens_recent": 0, "cost_recent": 0.0,
         "stage": "实现", "state": "recent", "project": "demo-app",
         "task": "单点实现：补一个边界用例"},
    ]
    inwin = [t for t in tasks if now - t["end"] <= window_s]
    kinds = ["dsh", "pi", "pi-rpc", "codex", "subagent"]
    aggs = {}
    for k in kinds:
        ts = [t for t in inwin if t["kind"] == k]
        aggs[k] = {"sessions": len([t for t in ts if t["state"] == "active"]),
                   "tokens_window": sum(t["tokens"] for t in ts),
                   "tokens_recent": sum(t["tokens_recent"] for t in ts),
                   "cost_window": sum(t["cost"] for t in ts),
                   "cost_recent": sum(t["cost_recent"] for t in ts),
                   "stage": next((t["stage"] for t in ts if t["state"] == "active"), None)}

    def mk(nid, label, role, layer, base, note):
        a = aggs.get(nid, {"sessions": 0, "tokens_window": 0, "tokens_recent": 0,
                           "cost_window": 0.0, "cost_recent": 0.0, "stage": None})
        busy = min(1.0, a["sessions"] * 0.55 + a["tokens_recent"] / 250_000)
        return {"id": nid, "label": label, "role": role, "layer": layer, "base": base,
                "busy": busy, **a, "note": note}

    proj_running = len([t for t in inwin if t.get("project") == "demo-app" and t["state"] == "active"])
    proj_tasks = len([t for t in inwin if t.get("project") == "demo-app"])
    nodes = [
        {"id": "neil", "label": "Neil", "role": "human", "layer": 0, "base": 46, "busy": 1.0,
         "sessions": 1, "tokens_window": 0, "tokens_recent": 0, "cost_window": 0.0, "cost_recent": 0.0,
         "stage": None, "note": "指令源"},
        {"id": "hermes", "label": "Hermes", "role": "orchestrator", "layer": 1, "base": 58, "busy": 1.0,
         "sessions": 1, "tokens_window": 0, "tokens_recent": 0, "cost_window": 0.0, "cost_recent": 0.0,
         "stage": "编排", "note": "调度 / 验收"},
        mk("dsh", "DSH headless", "worker", 2, 48, "沙箱 · 长链"),
        mk("pi", "Pi", "worker", 2, 40, "YOLO · 单点"),
        mk("pi-rpc", "Pi RPC", "worker", 2, 38, "常驻 · 可 steer"),
        mk("codex", "Codex CLI", "worker", 2, 36, "按需"),
        mk("subagent", "Hermes Subagent", "worker", 2, 38, "delegate_task"),
        {"id": "proj:demo-app", "label": "demo-app", "role": "project", "layer": 3, "base": 30,
         "busy": min(1.0, proj_running * 0.5 + proj_tasks * 0.1), "sessions": proj_running,
         "tokens_window": 0, "tokens_recent": 0,
         "cost_window": sum(t["cost"] for t in inwin if t.get("project") == "demo-app"),
         "cost_recent": 0.0, "stage": None, "note": f"{proj_tasks} 个任务在窗内"},
    ]
    edges = [{"id": "e:neil-hermes", "from": "neil", "to": "hermes", "kind": "request", "label": "指令",
              "state": "active", "started_at": now - 20 * 60, "ended_at": now, "idx": 0, "cost": 0.0, "tokens": 0}]
    for t in tasks:
        if t not in inwin:
            continue
        edges.append({"id": f"e:{t['key']}", "from": "hermes", "to": t["kind"], "kind": "dispatch",
                      "label": t["task"][:44], "full": t["task"], "state": t["state"],
                      "started_at": t["start"], "ended_at": t["end"], "idx": 0,
                      "tokens": t["tokens"], "cost": t["cost"], "tokens_recent": t["tokens_recent"],
                      "stage": t["stage"], "key": t["key"], "log": "", "project": t["project"] or None,
                      "evidence": "registry" if t["kind"] == "subagent" else "process"})
        if t["state"] != "recent" and t["project"]:
            edges.append({"id": f"e:{t['key']}:p", "from": t["kind"], "to": "proj:" + t["project"],
                          "kind": "workspace", "label": "", "state": t["state"],
                          "started_at": t["start"], "ended_at": t["end"], "idx": 0,
                          "tokens": 0, "cost": 0.0, "key": t["key"]})
    timeline = [{"key": t["key"], "kind": t["kind"], "project": t["project"], "label": t["task"],
                 "start": t["start"], "end": t["end"], "state": t["state"], "stage": t["stage"],
                 "tokens": t["tokens"], "cost": t["cost"], "log": ""} for t in inwin]
    return {"generated_at": now, "window_min": window_min, "live": True, "demo": True,
            "counts": {"active_tasks": len([t for t in inwin if t["state"] == "active"]),
                       "tokens_5m": sum(t["tokens_recent"] for t in inwin),
                       "cost_today": round(sum(t["cost"] for t in inwin), 3),
                       "cost_window": round(sum(t["cost"] for t in inwin), 3)},
            "nodes": nodes, "edges": edges, "timeline": timeline,
            "history_span": {"tasks": len(tasks), "from": min(t["start"] for t in tasks)}}


# ------------------------------------------------------------------------ HTTP

_CACHE = {"t": 0.0, "data": None, "window": 15}
_HIST = None


def get_history():
    global _HIST
    if _HIST is None:
        _HIST = load_history()
    return _HIST


def state_cached(window_min=15):
    now = now_ts()
    if _CACHE["data"] is None or now - _CACHE["t"] > 1.0 or _CACHE["window"] != window_min:
        _CACHE.update(t=now, data=build_state(window_min, get_history()), window=window_min)
    return _CACHE["data"]


class Handler(BaseHTTPRequestHandler):
    window = 15
    demo = False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.path.startswith("/api/state"):
            q = re.search(r"window=(\d+)", self.path)
            win = max(1, min(180, int(q.group(1)) if q else self.window))
            data = demo_state(win) if self.demo else state_cached(win)
            return self._send(200, json.dumps(data, ensure_ascii=False).encode())
        if self.path.startswith("/api/replay"):
            if self.demo:
                return self._send(200, json.dumps(demo_state(self.window), ensure_ascii=False).encode())
            q = re.search(r"t=([\d.]+)", self.path)
            if not q:
                return self._send(400, b'{"error":"need t"}')
            return self._send(200, json.dumps(replay_state(float(q.group(1)), get_history()),
                                              ensure_ascii=False).encode())
        if self.path.startswith("/api/history"):
            if self.demo:
                q = re.search(r"hours=(\d+)", self.path)
                hrs = int(q.group(1)) if q else 24
                cut = now_ts() - hrs * 3600
                rows = [{k: r.get(k) for k in ("key", "kind", "label", "project", "start", "end", "state",
                                               "tokens", "cost", "stage", "in", "out", "cache")}
                        for r in demo_history()["tasks"].values() if (r.get("start") or 0) >= cut]
                rows.sort(key=lambda r: r["start"] or 0)
                return self._send(200, json.dumps({"tasks": rows}, ensure_ascii=False).encode())
            q = re.search(r"hours=(\d+)", self.path)
            hrs = int(q.group(1)) if q else 24
            cut = now_ts() - hrs * 3600
            h = get_history()
            rows = [{k: r.get(k) for k in ("key", "kind", "label", "project", "start", "end", "state",
                                           "tokens", "cost", "stage", "in", "out", "cache")}
                    for r in h["tasks"].values() if (r.get("start") or 0) >= cut]
            rows.sort(key=lambda r: r["start"] or 0)
            return self._send(200, json.dumps({"tasks": rows}, ensure_ascii=False).encode())
        if path in ("/", "/index.html"):
            try:
                body = open(PAGE, "rb").read()
            except OSError:
                return self._send(500, b"agent_flow.html missing", "text/plain; charset=utf-8")
            return self._send(200, body, "text/html; charset=utf-8")
        self._send(404, b'{"error":"not found"}')

    def log_message(self, fmt, *args):
        pass


def main():
    try:
        import sys
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="agent/harness 工作流面板")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--window", type=int, default=15)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--history", type=int, help="打印 N 小时内历史任务")
    ap.add_argument("--backfill", type=int, nargs="?", const=7, metavar="DAYS",
                    help="从最近 N 天会话日志回填历史（默认 7 天）")
    ap.add_argument("--demo", action="store_true", help="用内置演示数据起服务（不读本机数据，适合截图/试用）")
    args = ap.parse_args()

    # 一次性初始化历史文件目录
    os.makedirs(DISPATCH_DIR, exist_ok=True)

    if args.backfill:
        n = backfill(args.backfill)
        print(f"回填完成：{n} 条任务记录 → {HIST_FILE}")
        return
    if args.history:
        cut = now_ts() - args.history * 3600
        h = load_history()
        for r in sorted([r for r in h["tasks"].values() if (r.get("start") or 0) >= cut],
                        key=lambda r: r.get("start") or 0):
            print(f"{datetime.fromtimestamp(r['start'], TZ):%m-%d %H:%M}  {r['kind']:<7} "
                  f"{(r.get('project') or '-'):<18} {r.get('state'):<8} "
                  f"¥{r.get('cost', 0):.3f}  {one_line(r.get('label'), 60)}")
        return
    if args.dump:
        print(json.dumps(build_state(args.window, get_history()), ensure_ascii=False, indent=2))
        return

    Handler.window = args.window
    Handler.demo = args.demo
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    tag = " (demo)" if args.demo else ""
    print(f"agent-flow: http://127.0.0.1:{args.port}/{tag}   (window={args.window}min)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
