#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""af_dispatch.py — 派活登记：写一条「父 → 子」事实记录，然后 exec 真正命令。

为什么要有它
============
  DSH headless / Pi 的会话日志里没有「谁派的」这一栏，采集端只能靠进程树和 cwd 猜。
  这个包装器在启动执行者之前先把 {parent, kind, label, task_file, ts, pid, argv}
  落成一份登记文件；随后 os.execvp 就地替换进程（pid 不变），所以登记里的 pid
  就是执行者进程（或它的 shell 包装）。采集端读到登记 = evidence:"registry"，
  读不到 = "process"/"inferred"。

用法
====
  # 登记 + 立即执行（pid 保持，exec 替换）
  ~/.hermes/scripts/af_dispatch.py --kind dsh --label "Step 12 缓存预热" \
      --task ~/code/my-app/docs/tasks/step12.md -- \
      bash -lc 'cd ~/code/my-app && node ... headless - < docs/tasks/step12.md'

  # 只看会写什么、不执行
  ~/.hermes/scripts/af_dispatch.py --kind pi --label "只读侦察" --dry-run -- pi -p "..."

  # 手工补记一条（已经跑完的活）
  ~/.hermes/scripts/af_dispatch.py --kind dsh --label "Step 17" --stamp-now --done
"""

import argparse
import json
import os
import time
import uuid

FLOW_DIR = os.path.expanduser("~/.hermes/state/agent_flow")
DISPATCH_DIR = os.path.join(FLOW_DIR, "dispatches")


def main():
    ap = argparse.ArgumentParser(description="登记一次派活再 exec")
    ap.add_argument("--kind", required=True, help="dsh / pi / codex / subagent")
    ap.add_argument("--label", required=True)
    ap.add_argument("--task", help="任务书路径")
    ap.add_argument("--parent", default="hermes")
    ap.add_argument("--session", help="Hermes 会话 id（可选）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--done", action="store_true", help="直接记成 finished（补记历史）")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = [c for c in args.cmd if c != "--"]
    os.makedirs(DISPATCH_DIR, exist_ok=True)
    did = "disp_" + uuid.uuid4().hex[:10]
    rec = {
        "id": did,
        "parent": args.parent,
        "parent_session": args.session,
        "kind": args.kind,
        "label": args.label,
        "task_file": os.path.abspath(os.path.expanduser(args.task)) if args.task else None,
        "ts": time.time(),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "argv": cmd,
        "state": "finished" if args.done else "started",
    }
    path = os.path.join(DISPATCH_DIR, f"{int(rec['ts'])}-{did}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)

    if args.dry_run:
        print(json.dumps({"record": path, "would_exec": cmd or None}, ensure_ascii=False, indent=1))
        return
    if not cmd:
        print(path)
        return
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
