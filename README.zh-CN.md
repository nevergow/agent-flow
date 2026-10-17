# Worker Dashboard —— 给编码 agent 用的像素风看板

[English](README.md) · **简体中文**

> 一页回答：**今天我把活派给了谁、谁还在跑、每一步花了多少、token 都去哪了。**

单文件、零依赖的本地看板。它读 DSH（headless）、Pi、Pi RPC 本来就会写在本机的
会话日志，渲染成一页纸色像素看板：KPI、14 天 token 流、7 天时间线、项目分布、
运行时长、派活拓扑、当前任务和全部运行流水。

![截图](docs/screenshot.png)

## 图上有什么

| 区块 | 内容 |
|---|---|
| 4 张 KPI 卡 | 在跑 / 今日派活次数 / 今日 token（含缓存读）/ 今日花费 |
| `TOKENS / 14D` | 按天堆叠 cache / input / output，叠加花费曲线 |
| `TIMELINE / 7D` | 每次运行画成本地 0–24 点的一条色带，按执行者着色 |
| `PROJECTS` | 每个工作区的 token、次数、占比 |
| `DURATION` | 最近若干次运行的实际耗时，按结果绿/黄/红 |
| `DELEGATION GRAPH` | 编排 → 执行者 → 任务节点 → 工作区，一次派活一条车道 |
| `NOW RUNNING` | 当前任务 + 最近几次工具调用 + 已运行时长 + token + 花费 |
| `RUN HISTORY` | 全部流水：时间 / 执行者 / 项目 / 任务 / 状态 / token（12 格量级条）/ 花费 / 耗时 |

刷新就是一个 `<meta http-equiv="refresh">`：没有前端框架、没有构建步骤、不联网。
每次请求都重新采集 + 重新渲染，所以页面上永远是"此刻的日志"。

## 快速开始

```bash
# 生成一份静态页到 ~/.hermes/cache/worker-dashboard-pixel.html 并打开
python3 worker_dashboard.py --style pixel --open

# 起实时看板（每次请求重新采集，页面自己每 5 秒刷新）
python3 worker_dashboard.py --style pixel --serve 8799
# → http://127.0.0.1:8799/

# 演示数据（完全不读本机，适合截图/第一次看效果）
python3 worker_dashboard.py --style pixel --demo --serve 8801
```

参数：

| 参数 | 说明 |
|---|---|
| `--style pixel` | 像素风看板（默认 `clean` 是朴素的浅色表格） |
| `--serve PORT` | 在 127.0.0.1 起服务，每次请求重新采集 |
| `--demo` | 合成会话数据，不读本机任何文件 |
| `--refresh N` | 自动刷新秒数（默认 5，0 = 关） |
| `--no-refresh` | 等价于 `--refresh 0` |
| `--out FILE` | 一次性生成的 HTML 路径 |

依赖：`python3`（只用标准库）+ `zstd` 命令行（`brew install zstd`）——
DSH 的会话文件是 zstd 压缩的 JSONL 事件流。

## 数据源

| 执行者 | 路径 |
|---|---|
| DSH headless | `~/.dsh/sessions/**/session.v4.jsonl.zstd` |
| Pi（一次性） | `~/.pi/agent/sessions/**/*.jsonl` |
| Pi RPC（常驻） | `~/.hermes/logs/pi-rpc-<name>.jsonl` |

全程只读：看板不会往任何会话目录写东西。状态（`RUNNING` / `done` / `interrupted`）
来自事件流和会话自己的记账，不靠推测。

## 成本口径

- **DSH** —— token 用量 × DeepSeek 官价：输入 ¥1/M、输出 ¥4/M、缓存命中 ¥0.02/M；
  北京时间工作日 9–12、14–18 高峰时段翻倍。
- **Pi / Pi RPC** —— 用 CLI 自己报的费用（美元），显示为 `$`。

这是估算不是账单：数字来自 agent 自己记的 usage；一次被续跑过的会话按它实际
发出的调用计费。

## 隐私

`--demo` 用固定种子生成一套虚构数据（三个假项目、24 条运行），所以上面这张
截图和任何公开演示里都没有真实项目名、路径或任务正文。正常模式不联网：页面
要么是本地文件，要么是 `127.0.0.1` 上的服务。

## 另一个视图

同仓库的 `agent_flow.py` 是第二套前端：用一份小 JSON 账本驱动的 SVG 拓扑，适合
回放昨天、看"父 → 子"的派活边，而不是一张平铺的运行表。两者读同一批 worker
日志，都不写。

## 字体

像素界面内嵌 [Press Start 2P](https://fonts.google.com/specimen/Press+Start+2P)
（SIL Open Font License 1.1，见 `assets/OFL.txt`）的 base64，离线渲染也一致；
字体文件缺失时回落到系统等宽字体。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
