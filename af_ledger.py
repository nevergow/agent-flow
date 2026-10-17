#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""af_ledger.py — Agent Flow 切片账本（SQLite 数据层）。

四张表：requests（需求）→ slices（切片）→ runs（运行）→ verdicts（验收）。
只做数据层：不联网、不依赖第三方包、不读除 ~/.hermes/state.db 之外的私有数据。

库路径
======
  默认  ~/.hermes/state/agent_flow/ledger.db
  覆盖  AF_LEDGER=/tmp/xxx.db

  import af_ledger 不建库、不写盘；只有写命令（以及 seed-demo/selftest 的临时库）才动磁盘。
  seed-demo 在 AF_LEDGER 未设置或指向默认库时拒绝执行（退出码 2）。

用法
====
  af_ledger.py new-request --source hermes --text "需求"
  af_ledger.py new-slice --req req_xxx --goal "切片" --executor pi --scope "a.py,b.py"
  af_ledger.py start-run --slice slc_xxx --kind pi --evidence registry
  af_ledger.py end-run   --run run_xxx --exit 0
  af_ledger.py verdict   --slice slc_xxx --result pass --reason "selftest"
  af_ledger.py list      [--since 7d] [--limit 50] [--json]
  af_ledger.py stats     [--since 7d] [--json]
  af_ledger.py show      SLICE_ID [--json]
  af_ledger.py sync-cron [--days 7] [--json]
  af_ledger.py seed-demo
  af_ledger.py selftest

时间统一存 epoch 浮点秒；文本模式只做本地时区展示。
"""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.parse import quote

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".hermes", "state", "agent_flow", "ledger.db")
HERMES_DB = os.path.join(HOME, ".hermes", "state.db")

SOURCES = ("hermes", "cron", "cli", "service")
EXECUTORS = ("dsh", "pi", "rpc", "subagent", "cron", "inline")
EVIDENCE = ("registry", "process", "inferred")
SLICE_STATES = ("planned", "running", "rejected", "passed", "aborted", "failed")
VERDICT_RESULTS = ("pass", "reject", "abort", "na")

SCHEMA = """
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
"""

REQUIRED_TABLES = frozenset(("requests", "slices", "runs", "verdicts"))


class LedgerError(Exception):
    """参数/约束错误（默认退出码 2）；数据库错误由 main() 映射到退出码 1。"""

    def __init__(self, message, exit_code=2):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# 路径与连接
# ---------------------------------------------------------------------------

def db_path():
    """生效的库路径（尊重 AF_LEDGER；空字符串按未设置处理）。"""
    env = os.environ.get("AF_LEDGER")
    if env and env.strip():
        return os.path.expanduser(env.strip())
    return DEFAULT_DB


def _abs_path(path):
    return os.path.abspath(os.path.expanduser(path))


def _norm_path(path):
    return os.path.realpath(_abs_path(path))


def _same_path(a, b):
    return _norm_path(a) == _norm_path(b)


def _ensure_schema(con):
    con.executescript(SCHEMA)


def _open_write(path=None):
    """打开（必要时创建）可写库；建目录、WAL、busy_timeout。"""
    p = _abs_path(path or db_path())
    parent = os.path.dirname(p)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(p, timeout=3.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(con)
    return con


def _open_ro(path=None):
    """只读打开；库不存在或没有账本表时返回 None（绝不建库）。"""
    p = _abs_path(path or db_path())
    if not os.path.isfile(p):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % quote(p), uri=True, timeout=3.0)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=3000")
        names = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
    except sqlite3.Error:
        con.close()
        return None
    if not REQUIRED_TABLES <= names:
        con.close()
        return None
    return con


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _now():
    return round(time.time(), 6)


def _epoch(value, name):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise LedgerError("--%s must be epoch seconds" % name)
    if not math.isfinite(v):
        raise LedgerError("--%s must be a finite epoch value" % name)
    return v


def _positive_int(value, name):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise LedgerError("--%s must be an integer" % name)
    if v < 1:
        raise LedgerError("--%s must be >= 1" % name)
    return v


def _parse_since(text):
    """'7d' / '24h' / '30d' → (seconds, 规范化标签)。"""
    if text is None:
        text = "7d"
    m = re.fullmatch(r"\s*(\d+)\s*([dh])\s*", str(text))
    if not m:
        raise LedgerError("invalid --since %r (expected e.g. 7d, 24h, 30d)" % text)
    n = int(m.group(1))
    unit = m.group(2)
    if n < 1:
        raise LedgerError("--since must be positive")
    secs = n * (86400 if unit == "d" else 3600)
    return secs, "%d%s" % (n, unit)


def _gen_id(con, table, prefix):
    for _ in range(100):
        cand = prefix + secrets.token_hex(5)  # 5 bytes = 10 hex chars
        if not con.execute("select 1 from %s where id=?" % table, (cand,)).fetchone():
            return cand
    raise LedgerError("cannot allocate a unique id for %s" % table)


def _resolve_id(con, table, prefix, wanted):
    if wanted is not None:
        wanted = wanted.strip()
        if not wanted:
            raise LedgerError("--id must not be empty")
        if con.execute("select 1 from %s where id=?" % table, (wanted,)).fetchone():
            raise LedgerError("id already exists: %s" % wanted)
        return wanted
    return _gen_id(con, table, prefix)


def _insert(con, sql, params, what):
    try:
        con.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise LedgerError("%s constraint failed: %s" % (what, exc)) from exc


def _normalize_artifacts(value):
    if value is None:
        return None
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return ",".join(parts) if parts else None


# ---------------------------------------------------------------------------
# 写命令
# ---------------------------------------------------------------------------

def new_request(source, text, id=None, ts=None):
    if source not in SOURCES:
        raise LedgerError("invalid --source %r (expected one of: %s)"
                          % (source, ", ".join(SOURCES)))
    text = text or ""
    if not text.strip():
        raise LedgerError("--text must not be empty")
    when = _epoch(ts, "ts") if ts is not None else _now()
    con = _open_write()
    try:
        with con:
            rid = _resolve_id(con, "requests", "req_", id)
            _insert(con,
                    "insert into requests(id, ts, source, text) values(?, ?, ?, ?)",
                    (rid, when, source, text), "request")
    finally:
        con.close()
    return {"ok": True, "id": rid, "ts": when}


def new_slice(req_id, goal, executor, scope=None, anchor=None, spec_path=None, id=None):
    if executor not in EXECUTORS:
        raise LedgerError("invalid --executor %r (expected one of: %s)"
                          % (executor, ", ".join(EXECUTORS)))
    req_id = (req_id or "").strip()
    goal = goal or ""
    if not req_id:
        raise LedgerError("--req must not be empty")
    if not goal.strip():
        raise LedgerError("--goal must not be empty")
    now = _now()
    con = _open_write()
    try:
        with con:
            sid = _resolve_id(con, "slices", "slc_", id)
            _insert(con,
                    "insert into slices(id, req_id, goal, executor, scope, anchor, spec_path,"
                    " attempt, state, created, updated)"
                    " values(?, ?, ?, ?, ?, ?, ?, 1, 'planned', ?, ?)",
                    (sid, req_id, goal, executor, scope, anchor, spec_path, now, now),
                    "slice")
    finally:
        con.close()
    return {"ok": True, "id": sid, "req_id": req_id, "executor": executor,
            "state": "planned", "attempt": 1}


def start_run(slice_id, kind, attempt=None, pid=None, evidence="registry", label=None):
    if kind not in EXECUTORS:
        raise LedgerError("invalid --kind %r (expected one of: %s)"
                          % (kind, ", ".join(EXECUTORS)))
    if evidence not in EVIDENCE:
        raise LedgerError("invalid --evidence %r (expected one of: %s)"
                          % (evidence, ", ".join(EVIDENCE)))
    if attempt is not None:
        attempt = _positive_int(attempt, "attempt")
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            raise LedgerError("--pid must be an integer")
    now = _now()
    con = _open_write()
    try:
        with con:
            row = con.execute("select attempt from slices where id=?", (slice_id,)).fetchone()
            if row is None:
                raise LedgerError("unknown slice: %s" % slice_id)
            want = attempt if attempt is not None else int(row["attempt"])
            rid = _gen_id(con, "runs", "run_")
            _insert(con,
                    "insert into runs(id, slice_id, attempt, kind, label, pid, start,"
                    " end, exit_code, evidence) values(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                    (rid, slice_id, want, kind, label, pid, now, evidence), "run")
            con.execute("update slices set state='running', updated=? where id=?",
                        (now, slice_id))
    finally:
        con.close()
    return {"ok": True, "id": rid, "slice_id": slice_id, "attempt": want,
            "kind": kind, "start": now}


def end_run(run_id, exit_code=None, end=None):
    if exit_code is not None:
        try:
            exit_code = int(exit_code)
        except (TypeError, ValueError):
            raise LedgerError("--exit must be an integer")
    when = _epoch(end, "end") if end is not None else _now()
    con = _open_write()
    try:
        with con:
            row = con.execute("select start from runs where id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError("unknown run: %s" % run_id)
            if when < float(row["start"]):
                raise LedgerError("--end precedes run start")
            if exit_code is None:
                con.execute("update runs set end=? where id=?", (when, run_id))
            else:
                con.execute("update runs set end=?, exit_code=? where id=?",
                            (when, exit_code, run_id))
    finally:
        con.close()
    return {"ok": True, "id": run_id, "end": when, "exit_code": exit_code}


def add_verdict(slice_id, result, reason=None, artifacts=None, retry=False,
                verified_by="hermes"):
    if result not in VERDICT_RESULTS:
        raise LedgerError("invalid --result %r (expected one of: %s)"
                          % (result, ", ".join(VERDICT_RESULTS)))
    if retry and result != "reject":
        raise LedgerError("--retry is only valid with --result reject")
    now = _now()
    con = _open_write()
    try:
        with con:
            row = con.execute("select attempt, state from slices where id=?",
                              (slice_id,)).fetchone()
            if row is None:
                raise LedgerError("unknown slice: %s" % slice_id)
            attempt = int(row["attempt"])
            vid = _gen_id(con, "verdicts", "vrd_")
            _insert(con,
                    "insert into verdicts(id, slice_id, attempt, result, reason, artifacts,"
                    " ts, verified_by) values(?, ?, ?, ?, ?, ?, ?, ?)",
                    (vid, slice_id, attempt, result, reason,
                     _normalize_artifacts(artifacts), now, verified_by), "verdict")
            state = row["state"]
            next_attempt = attempt
            if result == "pass":
                state = "passed"
            elif result == "reject":
                if retry:
                    state = "planned"
                    next_attempt = attempt + 1
                else:
                    state = "rejected"
            elif result == "abort":
                state = "aborted"
            # result == "na"：只记录一条不参与通过率的旁证，不改状态。
            con.execute("update slices set state=?, attempt=?, updated=? where id=?",
                        (state, next_attempt, now, slice_id))
    finally:
        con.close()
    return {"ok": True, "id": vid, "slice_id": slice_id, "attempt": attempt,
            "result": result, "state": state}


# ---------------------------------------------------------------------------
# 读接口（模块级，看板直接用；库不存在时返回空结构，不建库）
# ---------------------------------------------------------------------------

def _list_payload(con, since_label, cut, limit):
    sql = (
        "select s.id, s.req_id, s.goal, s.executor, s.state, s.attempt,"
        " s.created, s.updated, coalesce(q.text, '') as req_text,"
        " (select count(*) from runs r where r.slice_id = s.id) as runs_n,"
        " (select v.result from verdicts v where v.slice_id = s.id"
        "  order by v.ts desc, v.rowid desc limit 1) as last_verdict"
        " from slices s left join requests q on q.id = s.req_id"
        " where s.created >= ?"
        " order by s.created desc, s.id"
    )
    params = [cut]
    if limit is not None:
        sql += " limit ?"
        params.append(int(limit))
    rows = con.execute(sql, params).fetchall()
    slices = []
    for r in rows:
        slices.append({
            "id": r["id"],
            "goal": r["goal"],
            "executor": r["executor"],
            "state": r["state"],
            "attempt": int(r["attempt"]),
            "req_id": r["req_id"],
            "req_text": r["req_text"],
            "created": float(r["created"]),
            "updated": float(r["updated"]),
            "runs": int(r["runs_n"]),
            "last_verdict": r["last_verdict"],
            "cost_note": None,
        })
    return {"count": len(slices), "since": since_label, "slices": slices}


def read_slices(days=7, limit=50):
    """同 list --json。库不存在/无表时返回 {"count":0,"since":..,"slices":[]}。"""
    since_label = "%sd" % days
    try:
        cut = time.time() - float(days) * 86400
    except (TypeError, ValueError):
        return {"count": 0, "since": since_label, "slices": []}
    con = _open_ro()
    if con is None:
        return {"count": 0, "since": since_label, "slices": []}
    try:
        return _list_payload(con, since_label, cut, limit)
    finally:
        con.close()


def _empty_stats(since_label):
    return {
        "since": since_label,
        "slices_total": 0,
        "verdict_slices": 0,
        "passed_first_try": 0,
        "passed_after_retry": 0,
        "rejected": 0,
        "open": 0,
        "aborted": 0,
        "pass_rate_first_try": 0.0,
        "avg_attempts": 0.0,
        "cron_runs": 0,
        "cron_success_rate": 0.0,
        "by_executor": {},
        "avg_minutes": 0.0,
    }


def _stats_payload(con, since_label, cut):
    # 口径（与 list 的可见明细一致）：
    #   verdict_slices = 有实质裁决（result in pass/reject/abort）的切片数；
    #     result='na'（cron 自动同步）不算，否则会稀释通过率。
    #   pass_rate_first_try = passed_first_try / verdict_slices（分母 0 时 0.0）；
    #     passed_first_try 也只统计有实质裁决的切片，cron 的 na 不进分子/分母。
    #   avg_attempts 的分母同样只统计 verdict_slices。
    #   slices_total 仍是窗口内全部切片（含 cron、含未结案）。
    #   cron_runs = 窗口内 kind='cron' 的 run 数；
    #   cron_success_rate = executor='cron' 且 state='passed' 的切片数 /
    #     executor='cron' 的切片数（无 cron 切片时 0.0）。
    #   avg_minutes = 每个切片所有 run 的 (end-start) 之和的平均；
    #     完全没有 run 的切片不计；有 run 但尚未 end 的按 0 时长计。
    rows = con.execute(
        "select s.id, s.executor, s.state, s.attempt,"
        " (select count(*) from runs r where r.slice_id = s.id) as runs_n,"
        " (select coalesce(sum(r.end - r.start), 0) from runs r"
        "  where r.slice_id = s.id and r.end is not null) as dur_s,"
        " (select count(*) from verdicts v where v.slice_id = s.id"
        "  and v.result in ('pass','reject','abort')) as substantive_n"
        " from slices s where s.created >= ?",
        (cut,)).fetchall()

    total = len(rows)
    verdict_slices = 0
    passed_first = 0
    passed_retry = 0
    rejected = 0
    open_n = 0
    aborted = 0
    cron_total = 0
    cron_passed = 0
    attempts = []
    durations = []
    by_exec = {}

    for r in rows:
        state = r["state"]
        attempt = int(r["attempt"])
        substantive = int(r["substantive_n"]) > 0
        if substantive:
            verdict_slices += 1
            if state == "passed":
                if attempt == 1:
                    passed_first += 1
                else:
                    passed_retry += 1
            elif state == "rejected":
                rejected += 1
            attempts.append(attempt)
        if state in ("planned", "running"):
            open_n += 1
        elif state == "aborted":
            aborted += 1

        if int(r["runs_n"]) > 0:
            durations.append(float(r["dur_s"]) / 60.0)

        ex = r["executor"]
        if ex == "cron":
            cron_total += 1
            if state == "passed":
                cron_passed += 1
        bucket = by_exec.setdefault(ex, {"total": 0, "passed": 0, "rejected": 0})
        bucket["total"] += 1
        if state == "passed":
            bucket["passed"] += 1
        elif state == "rejected":
            bucket["rejected"] += 1

    cron_runs = int(_scalar(con,
                            "select count(*) from runs where kind='cron'"
                            " and start >= ?", (cut,)) or 0)

    payload = {
        "since": since_label,
        "slices_total": total,
        "verdict_slices": verdict_slices,
        "passed_first_try": passed_first,
        "passed_after_retry": passed_retry,
        "rejected": rejected,
        "open": open_n,
        "aborted": aborted,
        "pass_rate_first_try": (round(passed_first / verdict_slices, 4)
                                if verdict_slices else 0.0),
        "avg_attempts": (round(sum(attempts) / len(attempts), 2)
                         if attempts else 0.0),
        "cron_runs": cron_runs,
        "cron_success_rate": (round(cron_passed / cron_total, 4)
                              if cron_total else 0.0),
        "by_executor": {k: by_exec[k] for k in sorted(by_exec)},
        "avg_minutes": round(sum(durations) / len(durations), 2) if durations else 0.0,
    }
    return payload


def read_stats(days=7):
    """同 stats --json。库不存在/无表时返回全 0 结构。"""
    since_label = "%sd" % days
    try:
        cut = time.time() - float(days) * 86400
    except (TypeError, ValueError):
        return _empty_stats(since_label)
    con = _open_ro()
    if con is None:
        return _empty_stats(since_label)
    try:
        return _stats_payload(con, since_label, cut)
    finally:
        con.close()


def show_slice(slice_id, as_json=False):
    con = _open_ro()
    if con is None:
        raise LedgerError("unknown slice: %s" % slice_id)
    try:
        s = con.execute(
            "select id, req_id, goal, executor, scope, anchor, spec_path, attempt,"
            " state, created, updated from slices where id=?", (slice_id,)).fetchone()
        if s is None:
            raise LedgerError("unknown slice: %s" % slice_id)
        runs = con.execute(
            "select id, slice_id, attempt, kind, label, pid, start, end, exit_code,"
            " evidence from runs where slice_id=? order by start, id",
            (slice_id,)).fetchall()
        verdicts = con.execute(
            "select id, slice_id, attempt, result, reason, artifacts, ts, verified_by"
            " from verdicts where slice_id=? order by ts, rowid",
            (slice_id,)).fetchall()
    finally:
        con.close()

    payload = {
        "id": s["id"],
        "req_id": s["req_id"],
        "goal": s["goal"],
        "executor": s["executor"],
        "scope": s["scope"],
        "anchor": s["anchor"],
        "spec_path": s["spec_path"],
        "attempt": int(s["attempt"]),
        "state": s["state"],
        "created": float(s["created"]),
        "updated": float(s["updated"]),
        "runs": [dict(r) for r in runs],
        "verdicts": [dict(v) for v in verdicts],
    }
    if as_json:
        _emit(payload)
        return

    print("SLICE %s" % payload["id"])
    for key in ("goal", "executor", "state", "attempt", "req_id", "scope",
                "anchor", "spec_path", "created", "updated"):
        val = payload[key]
        if isinstance(val, float):
            val = "%s (%s)" % (_fmt_local(val), _fmt_num(val))
        print("  %-10s %s" % (key, "" if val is None else val))
    print("RUNS %d" % len(payload["runs"]))
    if payload["runs"]:
        rows = []
        for r in payload["runs"]:
            rows.append([
                r["id"], r["kind"], r["attempt"],
                _fmt_local(r["start"]),
                _fmt_local(r["end"]) if r["end"] is not None else "-",
                "-" if r["exit_code"] is None else r["exit_code"],
                r["evidence"],
            ])
        print(_table([("ID", 18), ("KIND", 9), ("ATT", 3), ("START", 16),
                      ("END", 16), ("EXIT", 4), ("EVIDENCE", 9)], rows,
                     {2: "right", 5: "right"}))
    print("VERDICTS %d" % len(payload["verdicts"]))
    if payload["verdicts"]:
        rows = []
        for v in payload["verdicts"]:
            rows.append([
                v["id"], v["result"], v["attempt"], _fmt_local(v["ts"]),
                v["verified_by"], v["reason"] or "", v["artifacts"] or "",
            ])
        print(_table([("ID", 18), ("RESULT", 7), ("ATT", 3), ("TS", 16),
                      ("BY", 8), ("REASON", 28), ("ARTIFACTS", 16)], rows,
                     {2: "right"}))


# ---------------------------------------------------------------------------
# sync-cron：只读 Hermes state.db，一个 cron job 一条需求 + 一个切片
# ---------------------------------------------------------------------------

_CRON_JOB_RE = re.compile(r"^cron_([0-9a-f]{6,})_")


def _cron_job_name(row):
    """display_name 非空 → 用它；否则取 title 第一个 " · " 之前；均无则 'cron'。"""
    name = (row["display_name"] or "").strip()
    if name:
        return name
    title = (row["title"] or "").strip()
    if title:
        head = title.split(" · ", 1)[0].strip()
        if head:
            return head
    return "cron"


def _cron_job_id(row, name):
    """从会话 id 捕获 cron job id；捕获不到则用 job 名的 sha1。"""
    m = _CRON_JOB_RE.match(row["id"] or "")
    if m:
        return m.group(1)
    return hashlib.sha1(name.encode("utf-8")).hexdigest()


def _cron_slice_id(job_id):
    return "slc_cron_" + hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:16]


def _cron_request_id(job_id):
    return "req_cron_" + hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:10]


def _cron_verdict_id(job_id):
    return "vrd_cron_" + hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:16]


def _cron_state(last):
    """最近一次 cron 会话的 end_reason → 切片状态（逐条映射，未知值兜底 failed）。"""
    reason = (last["end_reason"] or "").strip().lower()
    if reason.startswith("cron_complete"):
        return "passed"
    if reason.startswith(("cron_fail", "cron_error", "error", "failed")):
        return "failed"
    if reason.startswith(("cron_abort", "interrupt", "abort", "cancel")):
        return "aborted"
    if reason.startswith("cron_timeout"):
        return "failed"
    if last["ended_at"] is None:
        return "running"
    return "failed"


def sync_cron(days=7, as_json=False):
    days = _positive_int(days, "days")
    cut = time.time() - days * 86400
    warning = None
    rows = []
    try:
        hcon = sqlite3.connect("file:%s?mode=ro" % quote(_abs_path(HERMES_DB)),
                               uri=True, timeout=3.0)
        hcon.row_factory = sqlite3.Row
        try:
            rows = hcon.execute(
                "select id, source, display_name, title, started_at, ended_at, end_reason"
                " from sessions where source='cron' and started_at >= ?"
                " order by started_at, id", (cut,)).fetchall()
        finally:
            hcon.close()
    except Exception as exc:  # Hermes 库打不开：警告 + 退出码 0，不炸
        warning = "cannot read Hermes state.db: %s" % exc
        rows = []

    if warning:
        print("warning: %s" % warning, file=sys.stderr)

    groups = {}
    for r in rows:
        name = _cron_job_name(r)
        job_id = _cron_job_id(r, name)
        groups.setdefault(job_id, []).append((r, name))

    new_runs = 0
    if groups:
        con = _open_write()
        try:
            with con:
                for job_id in sorted(groups):
                    sessions = sorted(
                        groups[job_id],
                        key=lambda pair: (float(pair[0]["started_at"]),
                                          float(pair[0]["ended_at"] or 0),
                                          pair[0]["id"]))
                    last, name = sessions[-1]
                    state = _cron_state(last)
                    sid = _cron_slice_id(job_id)
                    req_id = _cron_request_id(job_id)
                    created = min(float(r["started_at"]) for r, _ in sessions)
                    updated = max(float(r["ended_at"] or r["started_at"])
                                  for r, _ in sessions)

                    # 一个 job 一条需求：ts 取该 job 最早会话的 started_at。
                    if not con.execute("select 1 from requests where id=?",
                                       (req_id,)).fetchone():
                        _insert(con,
                                "insert into requests(id, ts, source, text)"
                                " values(?, ?, 'cron', ?)",
                                (req_id, created, name), "cron request")

                    existing = con.execute(
                        "select state, updated from slices where id=?", (sid,)).fetchone()
                    if existing is None:
                        _insert(con,
                                "insert into slices(id, req_id, goal, executor, scope, anchor,"
                                " spec_path, attempt, state, created, updated)"
                                " values(?, ?, ?, 'cron', NULL, NULL, NULL, 1, ?, ?, ?)",
                                (sid, req_id, name, state, created, updated), "cron slice")
                    else:
                        # 不新建切片；把状态/更新时间推进到本次窗口内最新（不回退），
                        # 并补上可能为 NULL 的 req_id（需求 → 切片视图不留孤儿）。
                        new_updated = max(updated, float(existing["updated"]))
                        con.execute(
                            "update slices set req_id=?, goal=?, state=?, updated=?"
                            " where id=?",
                            (req_id, name, state, new_updated, sid))

                    for r, _ in sessions:
                        rid = "run_cron_%s" % r["id"]
                        start = float(r["started_at"])
                        end = None if r["ended_at"] is None else float(r["ended_at"])
                        found = con.execute("select end from runs where id=?", (rid,)).fetchone()
                        if found is None:
                            _insert(con,
                                    "insert into runs(id, slice_id, attempt, kind, label, pid,"
                                    " start, end, exit_code, evidence)"
                                    " values(?, ?, 1, 'cron', ?, NULL, ?, ?, NULL, 'cron')",
                                    (rid, sid, name, start, end), "cron run")
                            new_runs += 1
                        elif found["end"] is None and end is not None:
                            # 上次同步时会话还开着，这次已结束：只补 end，不重复插 run。
                            con.execute("update runs set end=? where id=?", (end, rid))

                    vid = _cron_verdict_id(job_id)
                    reason = last["end_reason"]  # 原样保留（未知值也不丢信息）
                    found_v = con.execute("select 1 from verdicts where id=?",
                                          (vid,)).fetchone()
                    if found_v is None:
                        _insert(con,
                                "insert into verdicts(id, slice_id, attempt, result, reason,"
                                " artifacts, ts, verified_by)"
                                " values(?, ?, 1, 'na', ?, NULL, ?, 'hermes')",
                                (vid, sid, reason, updated), "cron verdict")
                    else:
                        con.execute(
                            "update verdicts set reason=?, ts=? where id=?",
                            (reason, updated, vid))
        finally:
            con.close()

    payload = {
        "ok": True,
        "count": len(groups),
        "slices": len(groups),
        "sessions": len(rows),
        "new_runs": new_runs,
        "since_days": days,
    }
    if warning:
        payload["warning"] = warning

    if as_json:
        _emit(payload)
    else:
        print("cron slices: %d  sessions: %d  new runs: %d  window: %dd"
              % (payload["count"], payload["sessions"], new_runs, days))


# ---------------------------------------------------------------------------
# seed-demo：只允许写 AF_LEDGER 指定的非默认库
# ---------------------------------------------------------------------------

def _demo_id(prefix, key):
    return prefix + hashlib.sha1(
        ("af-ledger-demo:" + prefix + key).encode("utf-8")).hexdigest()[:10]


def seed_demo():
    env = os.environ.get("AF_LEDGER")
    if not env or not env.strip():
        raise LedgerError(
            "refusing to seed demo data into the default ledger (%s); "
            "set AF_LEDGER to a temporary path" % DEFAULT_DB)
    if _same_path(env, DEFAULT_DB):
        raise LedgerError(
            "refusing to seed demo data into the default ledger (%s); "
            "set AF_LEDGER to a temporary path" % DEFAULT_DB)

    now = time.time()
    base = now - 3 * 3600

    req_pass1 = _demo_id("req_", "pass-first")
    req_retry = _demo_id("req_", "pass-retry")
    req_rej = _demo_id("req_", "rejected")
    slc_pass1 = _demo_id("slc_", "pass-first")
    slc_retry = _demo_id("slc_", "pass-retry")
    slc_rej = _demo_id("slc_", "rejected")
    run_pass1 = _demo_id("run_", "pass-first")
    run_retry1 = _demo_id("run_", "pass-retry-1")
    run_retry2 = _demo_id("run_", "pass-retry-2")
    run_rej = _demo_id("run_", "rejected")
    vrd_pass1 = _demo_id("vrd_", "pass-first")
    vrd_retry_reject = _demo_id("vrd_", "pass-retry-reject")
    vrd_retry_pass = _demo_id("vrd_", "pass-retry-pass")
    vrd_rej = _demo_id("vrd_", "rejected")

    con = _open_write()
    try:
        with con:
            for table in ("verdicts", "runs", "slices", "requests"):
                con.execute("delete from %s" % table)

            con.executemany(
                "insert into requests(id, ts, source, text) values(?, ?, ?, ?)",
                [
                    (req_pass1, base - 600, "hermes", "演示：一次通过的切片"),
                    (req_retry, base - 2400, "cli", "演示：重试后通过的切片"),
                    (req_rej, base - 4200, "cron", "演示：被拒绝的切片"),
                ])
            con.executemany(
                "insert into slices(id, req_id, goal, executor, scope, anchor, spec_path,"
                " attempt, state, created, updated) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (slc_pass1, req_pass1, "演示：一次通过", "pi", "af_ledger.py",
                     "demoanchor1", "docs/tasks/ledger-A.md", 1, "passed", base, base + 2800),
                    (slc_retry, req_retry, "演示：重试后通过", "dsh", "worker_dashboard.py",
                     "demoanchor2", "docs/tasks/ledger-A.md", 2, "passed",
                     base - 1800, base + 1600),
                    (slc_rej, req_rej, "演示：被拒绝", "cron", "worker_runs.py",
                     "demoanchor3", "docs/tasks/ledger-A.md", 1, "rejected",
                     base - 3600, base - 2500),
                ])
            con.executemany(
                "insert into runs(id, slice_id, attempt, kind, label, pid, start, end,"
                " exit_code, evidence) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_pass1, slc_pass1, 1, "pi", "pi one-shot", 41001,
                     base + 60, base + 60 + 2700, 0, "registry"),
                    (run_retry1, slc_retry, 1, "dsh", "dsh attempt 1", 41002,
                     base - 1700, base - 1700 + 1200, 1, "process"),
                    (run_retry2, slc_retry, 2, "dsh", "dsh attempt 2", 41003,
                     base - 300, base - 300 + 1800, 0, "registry"),
                    (run_rej, slc_rej, 1, "cron", "cron nightly", None,
                     base - 3500, base - 3500 + 900, 2, "inferred"),
                ])
            con.executemany(
                "insert into verdicts(id, slice_id, attempt, result, reason, artifacts,"
                " ts, verified_by) values(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (vrd_pass1, slc_pass1, 1, "pass", "一次通过", "a.py,b.py",
                     base + 2800, "hermes"),
                    (vrd_retry_reject, slc_retry, 1, "reject", "首次验收失败",
                     "log1.txt", base - 400, "hermes"),
                    (vrd_retry_pass, slc_retry, 2, "pass", "重试后通过",
                     "log2.txt", base + 1600, "hermes"),
                    (vrd_rej, slc_rej, 1, "reject", "证据不足", "log3.txt",
                     base - 2500, "hermes"),
                ])
    finally:
        con.close()

    return {"ok": True, "seeded": 3, "db": _abs_path(db_path())}


# ---------------------------------------------------------------------------
# selftest：临时库，10 项断言
# ---------------------------------------------------------------------------

def _check(name, ok, detail=""):
    if ok:
        return 1
    print("FAIL: %s%s" % (name, (": " + detail) if detail else ""))
    raise SystemExit(1)


def _scalar(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return None if row is None else row[0]


def selftest():
    old = os.environ.get("AF_LEDGER")
    with tempfile.TemporaryDirectory(prefix="af_ledger_selftest_") as td:
        os.environ["AF_LEDGER"] = os.path.join(td, "ledger.db")
        try:
            checks = 0

            # 1) 建库 + 四张表
            con = _open_write()
            try:
                tables = {r[0] for r in con.execute(
                    "select name from sqlite_master where type='table'")}
                journal = _scalar(con, "pragma journal_mode")
            finally:
                con.close()
            checks += _check("schema", REQUIRED_TABLES <= tables
                             and str(journal).lower() == "wal",
                             "missing=%s journal_mode=%s"
                             % (sorted(REQUIRED_TABLES - tables), journal))

            # 2) new-request
            req = new_request("cli", "selftest request")
            con = _open_ro()
            try:
                got = _scalar(con, "select text from requests where id=?", (req["id"],))
            finally:
                con.close()
            checks += _check("new-request", req["id"].startswith("req_")
                             and len(req["id"]) == 14 and got == "selftest request")

            # 3) new-slice
            slc1 = new_slice(req["id"], "selftest slice one", "pi", scope="af_ledger.py")
            slc2 = new_slice(req["id"], "selftest slice two", "pi", scope="af_ledger.py")
            slc3 = new_slice(req["id"], "selftest open slice", "dsh")
            con = _open_ro()
            try:
                row = con.execute("select state, attempt, executor from slices where id=?",
                                  (slc1["id"],)).fetchone()
            finally:
                con.close()
            checks += _check("new-slice", row is not None and row["state"] == "planned"
                             and int(row["attempt"]) == 1 and row["executor"] == "pi")

            # 4) start-run
            run1 = start_run(slc1["id"], "pi", evidence="registry", label="first")
            con = _open_ro()
            try:
                row = con.execute("select kind, evidence, start, end from runs where id=?",
                                  (run1["id"],)).fetchone()
            finally:
                con.close()
            checks += _check("start-run", row is not None and row["kind"] == "pi"
                             and row["evidence"] == "registry"
                             and row["end"] is None and float(row["start"]) > 0)

            # 5) end-run
            end_ts = float(run1["start"]) + 60
            end_run(run1["id"], exit_code=0, end=end_ts)
            con = _open_ro()
            try:
                row = con.execute("select end, exit_code from runs where id=?",
                                  (run1["id"],)).fetchone()
            finally:
                con.close()
            checks += _check("end-run", row is not None
                             and abs(float(row["end"]) - end_ts) < 1e-6
                             and int(row["exit_code"]) == 0)

            # 6) verdict pass
            v1 = add_verdict(slc1["id"], "pass", reason="selftest", artifacts="a.py,b.py")
            con = _open_ro()
            try:
                state = _scalar(con, "select state from slices where id=?", (slc1["id"],))
                result = _scalar(con, "select result from verdicts where id=?", (v1["id"],))
            finally:
                con.close()
            checks += _check("verdict-pass", state == "passed" and result == "pass")

            # 7) verdict reject + --retry → attempt 递增、回到 planned
            run2a = start_run(slc2["id"], "pi")
            end_run(run2a["id"], exit_code=1, end=float(run2a["start"]) + 30)
            add_verdict(slc2["id"], "reject", reason="first try failed", retry=True)
            con = _open_ro()
            try:
                row = con.execute("select state, attempt from slices where id=?",
                                  (slc2["id"],)).fetchone()
            finally:
                con.close()
            checks += _check("verdict-reject-retry",
                             row is not None and row["state"] == "planned"
                             and int(row["attempt"]) == 2)

            # 补充测试数据：第二次尝试通过；slc3 保持 open，用于验证 stats 分母口径。
            run2b = start_run(slc2["id"], "pi")
            end_run(run2b["id"], exit_code=0, end=float(run2b["start"]) + 90)
            add_verdict(slc2["id"], "pass", reason="second try passed")

            # 8) stats 口径：verdict_slices 只算实质裁决（na 不计），open 仍在 slices_total
            st = read_stats(7)
            expected_exec = {
                "dsh": {"total": 1, "passed": 0, "rejected": 0},
                "pi": {"total": 2, "passed": 2, "rejected": 0},
            }
            checks += _check(
                "stats",
                st["slices_total"] == 3
                and st["verdict_slices"] == 2
                and st["passed_first_try"] == 1
                and st["passed_after_retry"] == 1
                and st["rejected"] == 0
                and st["open"] == 1
                and st["aborted"] == 0
                and st["pass_rate_first_try"] == 0.5
                and st["avg_attempts"] == 1.5
                and st["cron_runs"] == 0
                and st["cron_success_rate"] == 0.0
                and st["by_executor"] == expected_exec
                and st["avg_minutes"] == 1.5,
                "stats=%s" % json.dumps(st, ensure_ascii=False))

            # 9) list 的 runs 计数与 last_verdict
            lst = read_slices(7, 50)
            by_id = {s["id"]: s for s in lst["slices"]}
            checks += _check(
                "list-runs",
                lst["count"] == 3
                and by_id[slc1["id"]]["runs"] == 1
                and by_id[slc1["id"]]["last_verdict"] == "pass"
                and by_id[slc2["id"]]["runs"] == 2
                and by_id[slc2["id"]]["last_verdict"] == "pass"
                and by_id[slc3["id"]]["runs"] == 0
                and by_id[slc3["id"]]["last_verdict"] is None,
                "list=%s" % json.dumps(lst, ensure_ascii=False))

            # 10) 重复 --id 被拒（退出码 2 的约束错误）
            duplicate_rejected = False
            try:
                new_request("cli", "duplicate", id=req["id"])
            except LedgerError as exc:
                duplicate_rejected = exc.exit_code == 2
            checks += _check("duplicate-id", duplicate_rejected)

            if checks != 10:
                print("FAIL: check-count (expected 10, ran %d)" % checks)
                return 1
            print("SELFTEST OK (10 checks)")
            return 0
        finally:
            if old is None:
                os.environ.pop("AF_LEDGER", None)
            else:
                os.environ["AF_LEDGER"] = old


# ---------------------------------------------------------------------------
# 文本输出
# ---------------------------------------------------------------------------

def _fmt_num(v):
    """epoch → 去掉多余 0 的字符串（展示用，存储仍是 float）。"""
    return ("%.3f" % float(v)).rstrip("0").rstrip(".")


def _fmt_local(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
    except (TypeError, ValueError, OverflowError):
        return str(v)


def _cwidth(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _swidth(text):
    return sum(_cwidth(ch) for ch in str(text))


def _clip(text, width):
    text = str(text)
    if _swidth(text) <= width:
        return text
    if width <= 1:
        return "~" * max(0, width)
    out = []
    used = 0
    for ch in text:
        w = _cwidth(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "~"


def _pad(text, width, align="left"):
    text = _clip(text, width)
    gap = max(0, width - _swidth(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _table(headers, rows, aligns=None):
    aligns = aligns or {}
    widths = [w for _, w in headers]
    lines = ["  ".join(_pad(title, widths[i]) for i, (title, _) in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    for row in rows:
        cells = []
        for i, val in enumerate(row):
            cells.append(_pad(val, widths[i], aligns.get(i, "left")))
        lines.append("  ".join(cells))
    return "\n".join(line.rstrip() for line in lines)


def _print_list(payload):
    print("slices since %s: %d" % (payload["since"], payload["count"]))
    if not payload["slices"]:
        return
    rows = []
    for s in payload["slices"]:
        rows.append([
            s["id"], s["goal"], s["executor"], s["state"], s["attempt"],
            s["runs"], s["last_verdict"] or "-", _fmt_local(s["updated"]),
        ])
    print(_table(
        [("ID", 17), ("GOAL", 34), ("EXEC", 8), ("STATE", 8), ("ATT", 3),
         ("RUNS", 4), ("VERDICT", 7), ("UPDATED", 16)],
        rows, {4: "right", 5: "right"}))


def _print_stats(payload):
    print("stats since %s" % payload["since"])
    fields = [
        ("slices_total", payload["slices_total"]),
        ("verdict_slices", payload["verdict_slices"]),
        ("passed_first_try", payload["passed_first_try"]),
        ("passed_after_retry", payload["passed_after_retry"]),
        ("rejected", payload["rejected"]),
        ("open", payload["open"]),
        ("aborted", payload["aborted"]),
        ("pass_rate_first_try", payload["pass_rate_first_try"]),
        ("avg_attempts", payload["avg_attempts"]),
        ("cron_runs", payload["cron_runs"]),
        ("cron_success_rate", payload["cron_success_rate"]),
        ("avg_minutes", payload["avg_minutes"]),
    ]
    for key, val in fields:
        print("  %-22s %s" % (key, val))
    print("  by_executor")
    if not payload["by_executor"]:
        print("    (none)")
        return
    for name in sorted(payload["by_executor"]):
        b = payload["by_executor"][name]
        print("    %-10s total %-4d passed %-4d rejected %-4d"
              % (name, b["total"], b["passed"], b["rejected"]))


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse 默认会打印多行 usage；契约要求失败时 stderr 一行原因。"""

    def error(self, message):
        self.exit(2, "error: %s\n" % message)


def _build_parser():
    ap = _Parser(prog="af_ledger.py", description="Agent Flow 切片账本（SQLite）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new-request", help="登记一条需求")
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--text", required=True)
    p.add_argument("--id")
    p.add_argument("--ts", type=float)

    p = sub.add_parser("new-slice", help="登记一个切片")
    p.add_argument("--req", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--executor", required=True, choices=EXECUTORS)
    p.add_argument("--scope")
    p.add_argument("--anchor")
    p.add_argument("--spec")
    p.add_argument("--id")

    p = sub.add_parser("start-run", help="开始一次运行")
    p.add_argument("--slice", required=True)
    p.add_argument("--kind", required=True, choices=EXECUTORS)
    p.add_argument("--attempt", type=int)
    p.add_argument("--pid", type=int)
    p.add_argument("--evidence", choices=EVIDENCE, default="registry")
    p.add_argument("--label")

    p = sub.add_parser("end-run", help="结束一次运行")
    p.add_argument("--run", required=True)
    p.add_argument("--exit", dest="exit_code", type=int)
    p.add_argument("--end", type=float)

    p = sub.add_parser("verdict", help="登记验收结论")
    p.add_argument("--slice", required=True)
    p.add_argument("--result", required=True, choices=VERDICT_RESULTS)
    p.add_argument("--reason")
    p.add_argument("--artifacts")
    p.add_argument("--retry", action="store_true")
    p.add_argument("--verified-by", default="hermes")

    p = sub.add_parser("list", help="列出切片")
    p.add_argument("--since", default="7d")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stats", help="聚合统计")
    p.add_argument("--since", default="7d")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="查看单个切片")
    p.add_argument("slice_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sync-cron", help="从 Hermes state.db 同步 cron 会话")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--json", action="store_true")

    sub.add_parser("seed-demo", help="写入 3 条演示切片（需 AF_LEDGER 指向临时库）")
    sub.add_parser("selftest", help="临时库上跑 10 项自检")
    return ap


def _cmd_list(args):
    secs, label = _parse_since(args.since)
    limit = _positive_int(args.limit, "limit")
    payload = _list_payload_from_disk(label, time.time() - secs, limit)
    if args.json:
        _emit(payload)
    else:
        _print_list(payload)
    return 0


def _list_payload_from_disk(label, cut, limit):
    con = _open_ro()
    if con is None:
        return {"count": 0, "since": label, "slices": []}
    try:
        return _list_payload(con, label, cut, limit)
    finally:
        con.close()


def _cmd_stats(args):
    secs, label = _parse_since(args.since)
    con = _open_ro()
    if con is None:
        payload = _empty_stats(label)
    else:
        try:
            payload = _stats_payload(con, label, time.time() - secs)
        finally:
            con.close()
    if args.json:
        _emit(payload)
    else:
        _print_stats(payload)
    return 0


def _cmd_sync_cron(args):
    sync_cron(args.days, args.json)
    return 0


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "new-request":
            _emit(new_request(args.source, args.text, id=args.id, ts=args.ts))
        elif args.cmd == "new-slice":
            _emit(new_slice(args.req, args.goal, args.executor, scope=args.scope,
                            anchor=args.anchor, spec_path=args.spec, id=args.id))
        elif args.cmd == "start-run":
            _emit(start_run(args.slice, args.kind, attempt=args.attempt, pid=args.pid,
                            evidence=args.evidence, label=args.label))
        elif args.cmd == "end-run":
            _emit(end_run(args.run, exit_code=args.exit_code, end=args.end))
        elif args.cmd == "verdict":
            _emit(add_verdict(args.slice, args.result, reason=args.reason,
                              artifacts=args.artifacts, retry=args.retry,
                              verified_by=args.verified_by))
        elif args.cmd == "list":
            return _cmd_list(args)
        elif args.cmd == "stats":
            return _cmd_stats(args)
        elif args.cmd == "show":
            show_slice(args.slice_id, as_json=args.json)
        elif args.cmd == "sync-cron":
            return _cmd_sync_cron(args)
        elif args.cmd == "seed-demo":
            _emit(seed_demo())
        elif args.cmd == "selftest":
            return selftest()
        else:  # pragma: no cover - argparse guarantees a known command
            raise LedgerError("unknown command: %s" % args.cmd)
        return 0
    except LedgerError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except sqlite3.Error as exc:
        print("database error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("io error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
