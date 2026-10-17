# Agent Flow · 本地 agent 工作流看板

[English](README.md) · **简体中文**

> 一张图看清：**谁在干活、活是谁派的、干到哪一步、花了多少钱。**

把本机上跑着的编码 agent 会话（Hermes / DSH headless / Pi / Pi RPC / Codex /
子 agent）读出来，画成一张"节点 + 派活边"的实时拓扑。

![screenshot](docs/screenshot.png)

## 它解决什么

同时开几个编码 agent，终端里只剩碎片日志：不知道谁在跑、卡在哪一步、哪条任务是
谁派的、今天到底烧了多少钱。Agent Flow 把这些会话日志读出来，画成一张实时拓扑：

- 节点 = 参与者：你 → 编排主循环 → 执行者（沙箱长链 / 单点 / 常驻 / 子 agent）→ 工作区
- 边 = 一次派活，单向；同一对节点之间可以有多条（每一步一条，曲率自动错开）
- 只有**正在跑**的边有粒子流，新派活会在目标端打一圈脉冲——不动的东西不占注意力
- 底部是窗口内派活的甘特条 + 24 小时回放滑条

## 快速开始

零依赖，Python 3.9+ 标准库即可；`zstd` 命令用于读 DSH 的压缩会话（没有就自动跳过 DSH）。

```bash
git clone <this-repo> agent-flow && cd agent-flow

python3 agent_flow.py --demo        # 内置演示数据（虚构 demo-app），先看效果
# 浏览器打开 http://127.0.0.1:8799/

python3 agent_flow.py               # 读本机真实数据
python3 agent_flow.py --port 9000 --window 30
```

参数：

| 参数 | 作用 |
|---|---|
| `--port` | 监听端口，默认 8799（只绑 127.0.0.1） |
| `--window` | 时间窗（分钟），默认 15 |
| `--demo` | 用内置演示数据，不读任何本机文件 |
| `--backfill [天]` | 从历史会话日志回填（分钟粒度，默认 7 天），让回放立刻有内容 |
| `--history [小时]` | 在终端打印历史任务表（含成本） |
| `--dump` | 打印一次实时快照 JSON（调试采集用） |

页面参数：`?static=1` 关闭动画（截图 / 低功耗）。

## 图怎么读

- **列**：`指令 INGRESS → 编排 ORCHESTRATOR → 执行者 WORKERS → 工作区 WORKSPACE`；
  没有任务在跑的执行者会收进右侧「待命 IDLE」池，不占主视图
- **卡片**：名字 / 状态胶囊（`RUN` 在跑、`WAIT` 跑完待验收、`IDLE` 空闲）/ 当前阶段 /
  会话数 / 近 5 分钟 token / 窗口内花费 / 底部负载条
- **边**：一次派活；同一对节点之间的多条边按曲率错开；箭头单向
- **阶段**：从最近 3 分钟的工具名推断——`侦察`（只读）/ `实现`（写改）/ `自检`（测试命令）/ `收尾`
- **点节点或边**：看详情，并给出直接可用的命令（`worker_runs.py show <key>` / `tail -f <log>`）

## 数据来源（全部只读，不写任何会话文件）

| 来源 | 默认路径 | 取到什么 |
|---|---|---|
| DSH headless | `~/.dsh/sessions/<cwd>/session-*/session.v*.jsonl.zstd` | 任务书、turn/step、每次调用 usage、工具调用 |
| Pi（一次性） | `~/.pi/agent/sessions/<cwd>/*.jsonl` | 用户任务、每轮 usage |
| Pi RPC（常驻） | `~/.hermes/logs/pi-rpc-<name>.jsonl` | 事件流、turn_end usage |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | `token_count` 累计值、工具调用、cwd |
| Hermes 子 agent | `~/.hermes/state.db` → `async_delegations` | 真实父子关系、状态、`result_json` 里的 tokens/cost |
| Hermes 主循环活性 | `~/.hermes/logs/agent.log` | 最后一次 API 调用 / 工具调用时间 |

历史与回放存在 `~/.hermes/state/agent_flow/tasks.json`（每 15 秒一个采样点，保留 7 天）。

## 成本口径

所有金额都是**本地估算**，不是账单：

- DSH / Pi / Pi RPC / Codex：按 deepseek-flash 官方价换算
  （输入 ¥1、输出 ¥4、缓存命中 ¥0.02 每百万 token；北京时间工作日
  09:00–12:00 / 14:00–18:00 双倍），逐次调用累加
- 子 agent：优先用 `result_json.tokens` 按同一价目表换算；没有明细时退回
  Hermes 自报的 `cost_usd × 7.1`
- token 数含缓存命中——它衡量的是**吞吐**，不是"新产生的思考量"

## 派活登记：把"谁派的"变成事实

默认情况下"谁派的"只能靠进程树和 cwd 推断（图上标 `推定`）。用 `scripts/af_dispatch.py`
包一层，先写一条登记再 `exec` 真命令（pid 不变），采集端就能标 `登记`：

```bash
scripts/af_dispatch.py --kind dsh --label "Step 12 缓存预热" \
    --task docs/tasks/step12.md -- \
    bash -lc 'cd ~/code/my-app && node ... headless - < docs/tasks/step12.md'
```

登记文件落在 `~/.hermes/state/agent_flow/dispatches/*.json`。

## 已知边界

- **DSH 内部再起的子 agent 看不到**：会话日志里没有可关联的父指针
- **解析强绑定当前版本**：DSH 会话 v4、Pi 1.x、Codex rollout v1；上游格式变了要跟着改
- **只面向本机**：没有鉴权、没有多机聚合，服务只绑 `127.0.0.1`
- 系统级指标（CPU/内存）不采集，这里只关心"谁在干活"

## 目录

```
agent_flow.py       采集 + 内置 HTTP 服务（标准库）
agent_flow.html     单页前端（原生 JS + SVG，无构建步骤）
scripts/af_dispatch.py  派活登记包装器
docs/screenshot.png 演示截图（--demo 数据）
```

## License

MIT
