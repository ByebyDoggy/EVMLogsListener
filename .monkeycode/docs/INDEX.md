# EVM Chain Listener

轻量级EVM兼容链区块日志监听器，通过多RPC节点轮询机制获取区块日志信息。

## 项目概述

EVM Chain Listener 是一个后台服务，用于监控EVM兼容链（Ethereum、BNB Chain、Polygon等）的区块日志事件。服务通过轮询多个RPC节点获取新区块的日志信息，支持高可用部署，能够自动处理RPC节点的速率限制、网络异常等情况。

## 核心能力

- **多链支持**：同时监听多条EVM兼容链
- **多RPC节点冗余**：支持配置多个RPC节点，自动故障切换
- **智能轮询**：可配置查询间隔（15s/30s/60s），避免API限流
- **二分法补全**：日志量超限时自动使用二分法拆分查询
- **Webhook通知**：日志事件实时推送到指定Webhook endpoint
- **Docker部署**：一键Docker部署，开箱即用

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| 编程语言 | Python 3.10+ |
| RPC通信 | eth-abel 系列库 |
| 配置管理 | YAML |
| 日志输出 | JSON格式 |
| 部署方式 | Docker |

## 文档结构

```
.monkeycode/docs/
├── INDEX.md              # 本文档
├── ARCHITECTURE.md       # 系统架构设计
├── INTERFACES.md         # 接口定义
└── DEVELOPER_GUIDE.md   # 开发者指南
```

## 快速开始

详见 [DEVELOPER_GUIDE.md](./DEVELOPER_GUIDE.md)

## 项目状态

- 状态：规划中
- 版本：v0.1.0
