# EVM Chain Listener

一个轻量级的 EVM 兼容区块链事件日志监听器，支持**实时监听（Realtime）**和**历史回测（Backtest）**双模式运行，内置 LogPusher 可将日志推送至 AlertProcessor 进行告警处理，并支持**录制（Record）**与**回放（Replay）**功能。

## 功能特性

- **双模式运行**: 实时模式（增量轮询）+ 回测模式（批量历史查询），共享同一套 RPC 池与缓存
- **多链支持**: 同时监听多条 EVM 兼容链（Ethereum、BSC、Polygon 等）
- **多 RPC 节点池**: 支持配置多个 RPC 节点，按优先级自动故障转移
- **智能查询**: 二分查询算法处理 `eth_getLogs` 结果过大错误
- **速率限制**: 内置请求限流，保护公共/付费 RPC 节点
- **LogPusher**: 缓冲式 HTTP 推送，支持定时触发 + 阈值触发双重策略，自动重试与断线重连
- **录制与回放**: 将日志录制到本地 JSONL 文件，回测模式下可从本地文件回放，无需调用 RPC API
- **内存缓存**: FIFO 缓存存储日志，支持分页、多条件筛选查询
- **Web 界面**: 内置日志查看和筛选前端
- **REST API**: FastAPI 驱动的高性能 HTTP 接口，含回测任务管理、录制/回放管理、Pusher 统计等
- **跨平台**: 支持 Windows、Linux、macOS

---

## 快速开始

### 环境要求

- Python 3.10+
- pip 包管理器

### 安装依赖

```bash
# 克隆项目
git clone <repository-url>
cd EVMLogListener

# 创建虚拟环境（必须）
python -m venv .venv

# 激活虚拟环境
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

> **注意**: 项目规则要求必须使用虚拟环境运行。如果无法创建虚拟环境，才使用全局环境。

### 配置文件

复制配置模板并按需修改：

```bash
cp config.yaml.example config.yaml
```

详细配置说明见下方 [配置项详解](#配置项详解) 章节。

---

## 运行模式

### 一、实时模式（Realtime）

实时模式会持续轮询链上新区块，将捕获的日志写入本地缓存，并可选择性推送到 AlertProcessor。

#### 启动命令

```bash
# 进入 src 目录运行
cd src

# Linux / macOS
python -m evm_chain_listener.main -c ../config.yaml -m realtime

# Windows（使用虚拟环境）
..\..\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml -m realtime
```

#### 启动后行为

1. 初始化 `LogCache` 内存缓存（默认最大 10000 条）
2. 为 `config.yaml` 中配置的每条链创建 `ChainListener`，启动增量轮询
3. 如果启用了 AlertProcessor（`alert_processor.enabled: true`），初始化 `LogPusher`
4. **可选**: 若 `reconnect_check_on_startup: true`，启动时调用 AlertProcessor 的 `/ingest/status` 检测缺口并补发
5. 启动 FastAPI 服务，默认监听 `http://0.0.0.0:8080`

访问 http://localhost:8080 查看 Web 界面，访问 http://localhost:8080/docs 查看交互式 API 文档。

---

### 二、回测模式（Backtest）

回测模式用于查询指定区块范围内的历史日志。启动后不会自动执行查询，需通过 **API 提交回测任务**。

#### 启动命令

```bash
cd src

# Linux / macOS
python -m evm_chain_listener.main -c ../config.yaml -m backtest

# Windows（使用虚拟环境）
..\..\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml -m backtest
```

#### 回测模式工作流程

```
1. 启动服务（backtest 模式）        2. 通过 API 提交回测任务
   ↓                                  ↓
为每条链创建 BacktestRunner    →   POST /api/backtest/run
   ↓                               (chain, from_block, to_block)
等待任务提交...                     ↓
                              后台异步分批查询 [from_block, to_block]
                                   ↓
3. 轮询任务状态                      每批结果写入 LogCache + Pusher
   GET /api/backtest/status/{task_id}     ↓
   ↓                            任务完成/失败，可查看结果
返回进度、已发现日志数等
```

#### 提交回测任务示例

```bash
# 查询以太坊主网 19000000 ~ 19001000 区间的日志
curl -X POST "http://localhost:8080/api/backtest/run?chain=ethereum&from_block=19000000&to_block=19001000&batch_size=1000"

# 使用 chain ID 代替名称
curl -X POST "http://localhost:8080/api/backtest/run?chain=1&from_block=19000000&to_block=19001000"
```

响应：

```json
{
  "status": "submitted",
  "data": {
    "task_id": "a1b2c3d4e5f6",
    "chain_name": "ethereum",
    "chain_id": 1,
    "from_block": 19000000,
    "to_block": 19001000,
    "status": "pending",
    "total_batches": 11,
    "completed_batches": 0,
    "logs_found": 0,
    "progress_current_block": null,
    "started_at": "2026-01-01T12:00:00Z",
    "completed_at": null,
    "error": null
  }
}
```

#### 查询任务进度

```bash
curl "http://localhost:8080/api/backtest/status/a1b2c3d4e5f6"
```

#### 查看可用链列表

```bash
curl "http://localhost:8080/api/backtest/chains"
```

#### 查看所有任务

```bash
curl "http://localhost:8080/api/backtest/tasks"
```

#### 回测参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `chain` | string | 是 | 链名称（如 `ethereum`）或 chain ID（如 `1`），必须在 config.yaml 中已配置 |
| `from_block` | int | 是 | 起始区块号（含）|
| `to_block` | int | 是 | 终止区块号（含），必须 >= from_block |
| `batch_size` | int | 否 | 每批查询的区块数量，默认 `1000`，范围 `[10, 100000]` |

---

### 三、录制模式（Record）

录制模式可在实时模式或回测模式下启用，将捕获的日志自动写入本地 JSONL 文件，便于后续离线回放。

#### 启用录制

在 `config.yaml` 中配置 `recorder` 段：

```yaml
recorder:
  enabled: true                # 启用录制
  directory: "./recordings"    # JSONL 文件存储目录（自动创建）
```

#### 工作原理

```
RPC 节点 / 本地文件 ──→ _on_logs_received()
                              │
                   ┌──────────┤──────────┐
                   ▼          ▼          ▼
             LogCache    LogPusher   LogRecorder
             （缓存）    （推送）   （写入 JSONL）
                                        │
                            按链分组，每链一个 .jsonl 文件
                                        │
                            关闭时自动生成 .manifest.json
```

#### 录制文件格式

- **JSONL 文件**: `<chain_name>_<timestamp>.jsonl`，每行一条日志（`Log.to_dict()` 格式，hex 编码数字字段）
- **Manifest 文件**: `<chain_name>_<timestamp>.manifest.json`，记录元信息：

```json
{
  "chain_name": "ethereum",
  "chain_id": 1,
  "from_block": 19000000,
  "to_block": 19001000,
  "total_logs": 1234,
  "file": "ethereum_20260409_120000_000000.jsonl",
  "recorded_at": "2026-04-09T12:00:00+00:00"
}
```

---

### 四、回放模式（Replay）

回放模式在回测（backtest）模式下使用，从本地 JSONL 文件加载日志，**无需调用 RPC API**，适合离线分析、重复回测等场景。

#### 配置回放

在 `config.yaml` 中配置 `replay` 段：

```yaml
# 方式一：指定单个文件
replay:
  file_path: "./recordings/ethereum_20260409_120000_000000.jsonl"
  from_block: null    # 可选：过滤起始区块
  to_block: null      # 可选：过滤终止区块

# 方式二：指定目录（自动发现所有录制文件）
replay:
  directory: "./recordings"
  from_block: 19000000
  to_block: 19001000
```

#### 启动命令

```bash
cd src

# Linux / macOS
python -m evm_chain_listener.main -c ../config.yaml -m backtest

# Windows
..\..\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml -m backtest
```

当 `replay` 配置存在时，回测模式会自动：
1. 创建不带 RPC 池的 `BacktestRunner`（`use_rpc=False`）
2. 从指定文件/目录加载 JSONL 日志
3. 通过 `Log.from_dict()` 反序列化，确保数据结构与实时/回测模式一致
4. 将日志注入缓存和 Pusher，与正常运行流程完全相同

#### 通过 API 管理回放

```bash
# 列出可用录制文件
curl "http://localhost:8080/api/replay/recordings"

# 启动回放（指定文件和链）
curl -X POST "http://localhost:8080/api/replay/start?file_path=./recordings/ethereum_20260409_120000_000000.jsonl&chain=ethereum"

# 带区块范围过滤的回放
curl -X POST "http://localhost:8080/api/replay/start?file_path=./recordings/ethereum_20260409_120000_000000.jsonl&from_block=19000000&to_block=19001000"
```

#### 数据一致性保证

录制与回放使用相同的数据模型（`Log`），确保完整往返一致性：

| 步骤 | 方法 | 说明 |
|------|------|------|
| 录制 | `Log.to_dict()` | 序列化为 hex 编码的 JSON 字段 |
| 回放 | `Log.from_dict()` | 自动处理 hex/整数字段，还原完整 `Log` 对象 |

---

## 配置项详解

### 完整配置文件结构

```yaml
chains:                          # 链路配置（数组，支持多条链）
  - name: ethereum               # 链名称（必填，用于标识和 API 引用）
    chain_id: 1                  # 链 ID（必填，整数）
    poll_interval: 30            # 实时模式：轮询间隔（秒），仅 realtime 模式生效
    rpc_nodes:                   # RPC 节点列表（至少配置 1 个）
      - url: "https://..."       # RPC 节点 URL（必填，支持 ${ENV_VAR} 环境变量替换）
        priority: 1              # 优先级（数字越小越优先），默认 100
        type: public             # 节点类型：public / paid，用于标记
        rate_limit:              # 速率限制配置
          requests_per_second: 5 # 每秒请求数
          burst: 10              # 突发请求数

cache:                           # 本地内存缓存
  max_size: 10000                # 最大缓存日志条数（FIFO 淘汰）

api:                             # Web 服务配置
  host: "0.0.0.0"                # 监听地址
  port: 8080                     # 监听端口

log:                             # 日志配置
  level: INFO                    # 日志级别：DEBUG / INFO / WARNING / ERROR
  format: json                   # 输出格式：json / text
  output: stdout                 # 输出目标：stdout / 文件路径

health:                          # 健康检查
  enabled: true                  # 是否启用健康检查端点
  endpoint: /health              # 健康检查路径

alert_processor:                 # AlertProcessor / LogPusher 配置
  enabled: false                 # 是否启用日志推送（false 则不推送，仅本地缓存）
  url: "http://localhost:8000"   # AlertProcessor 服务地址
  push_interval_seconds: 5       # 定时触发间隔（秒）：每隔 N 秒刷一次缓冲区
  batch_size: 200                # 阈值触发：缓冲区积累 >= N 条日志时立即发送
  max_payload_mb: 10             # 单次请求最大载荷大小（MB），超出则拆分
  retry_attempts: 3              # 失败重试次数
  retry_base_delay_sec: 1        # 重试基础延迟（秒），指数退避：delay = base * 2^attempt
  timeout_seconds: 10            # HTTP 请求超时时间（秒）
  reconnect_check_on_startup: true  # 启动时是否检测 AlertProcessor 缺口并补发
  replay_endpoint: "/ingest/logs/replay"  # 补发端点路径

recorder:                        # 日志录制配置
  enabled: false                 # 是否启用录制（realtime / backtest 模式均生效）
  directory: "./recordings"      # JSONL 文件存储目录（自动创建）

replay:                          # 日志回放配置（仅 backtest 模式生效）
  file_path: null                # 单文件模式：指定 JSONL 文件路径
  directory: null                # 目录模式：自动发现目录下的录制文件（与 file_path 二选一）
  from_block: null               # 可选：过滤起始区块号
  to_block: null                 # 可选：过滤终止区块号
```

### 配置项速查表

| 配置路径 | 说明 | 默认值 | 必填 |
|----------|------|--------|------|
| `chains[].name` | 链名称 | — | 是 |
| `chains[].chain_id` | 链 ID | — | 是 |
| `chains[].poll_interval` | 实时模式轮询间隔（秒）| 30 | 否 |
| `chains[].rpc_nodes[].url` | RPC URL | — | 是 |
| `chains[].rpc_nodes[].priority` | 节点优先级 | 100 | 否 |
| `chains[].rpc_nodes[].type` | 节点类型 | public | 否 |
| `chains[].rpc_nodes[].rate_limit.requests_per_second` | QPS 限制 | 5 | 否 |
| `cache.max_size` | 缓存容量 | 10000 | 否 |
| `api.host` | API 地址 | 0.0.0.0 | 否 |
| `api.port` | API 端口 | 8080 | 否 |
| `log.level` | 日志级别 | INFO | 否 |
| `log.format` | 日志格式 | json | 否 |
| `alert_processor.enabled` | 启用推送 | false | 否 |
| `alert_processor.url` | 目标服务地址 | — | 启用时必填 |
| `alert_processor.push_interval_seconds` | 定时推送间隔 | 5 | 否 |
| `alert_processor.batch_size` | 阈值推送大小 | 200 | 否 |
| `alert_processor.retry_attempts` | 重试次数 | 3 | 否 |
| `alert_processor.timeout_seconds` | HTTP 超时 | 10 | 否 |
| `alert_processor.reconnect_check_on_startup` | 启动缺口检测 | true | 否 |
| `recorder.enabled` | 启用日志录制 | false | 否 |
| `recorder.directory` | 录制文件存储目录 | ./recordings | 否 |
| `replay.file_path` | 回放单文件路径 | — | 否 |
| `replay.directory` | 回放目录（自动发现） | — | 否 |
| `replay.from_block` | 回放起始区块过滤 | — | 否 |
| `replay.to_block` | 回放终止区块过滤 | — | 否 |

### 环境变量替换

配置文件中的任何字符串值都支持 `${ENV_VAR}` 语法进行环境变量替换：

```yaml
rpc_nodes:
  - url: "${ALCHEMY_ETH_URL}"       # 从环境变量读取
    priority: 1
  - url: "${INFURA_ETH_URL}"
    priority: 2
```

---

## CLI 参数

```
usage: python -m evm_chain_listener.main [-h] [-c CONFIG] [-m {realtime,backtest}] [-v] [--host HOST] [--port PORT]

options:
  -c, --config    配置文件路径（默认: config.yaml）
  -m, --mode      运行模式: realtime（实时监听）或 backtest（历史回测）（默认: realtime）
  --host          覆盖 API 监听地址
  --port          覆盖 API 监听端口
  -v, --version   显示版本号
```

---

## LogPusher 工作原理

当 `alert_processor.enabled: true` 时，LogPusher 会自动集成到数据流中：

### 数据流

```
RPC 节点 ──→ ChainListener / BacktestRunner ──→ _on_logs_received()
                                                    │
                                    ┌───────────────┤
                                    ▼               ▼
                              LogCache         LogPusher.buffer
                              （本地缓存）     （待推送缓冲区）
                                                    │
                                        ┌───────────┤
                                        ▼           ▼
                                   定时触发      阈值触发
                                 (每 N 秒)    (>= batch_size 条)
                                        │
                                        ▼
                                按 chain_id 分组
                                POST /ingest/logs
                                        │
                                   重试策略:
                                   · 429 → 读 Retry-After 等待后重试
                                   · 400 → 不重试（格式问题，丢弃本批次）
                                   · 5xx → 指数退避重试
                                   · timeout → 保留缓冲区，下次再发
```

### 双触发策略

| 触发方式 | 条件 | 说明 |
|----------|------|------|
| **定时触发** | 每 `push_interval_seconds` 秒 | 后台循环定时刷新 |
| **阈值触发** | 缓冲区 >= `batch_size` 条 | 新日志到达时立即触发 |

两者谁先满足就先执行，避免日志积压或频繁小包推送。

### 断线重连 / 缺口补发

启动时若 `reconnect_check_on_startup: true`：

1. 调用 `GET {alert_processor.url}/ingest/status`
2. 获取各链已消费的最新区块号 (`consumed_blocks`)
3. 对比本地缓存中该区块之后的日志
4. 如有缺口，调用 `POST {url}/ingest/logs/replay` 补发

### 推送统计

可通过 API 实时查看推送状态：

```bash
curl http://localhost:8080/api/pusher/stats
```

---

## API 接口总览

访问 `http://localhost:8080/docs` 查看交互式 Swagger 文档。

### 日志查询

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/logs` | 查询日志列表（支持多条件筛选 + 分页）|
| GET | `/api/logs/stats` | 缓存统计信息 |
| GET | `/api/logs/topics` | 已有的事件 topic 列表 |

**GET /api/logs 参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `chain_id` | int | 按 chain ID 筛选 |
| `from_block` | int | 起始区块 |
| `to_block` | int | 终止区块 |
| `from_time` | string | 起始时间（ISO8601）|
| `to_time` | string | 终止时间（ISO8601）|
| `address` | string | 合约地址筛选 |
| `topic` | string | 事件 topic 筛选 |
| `page` | int | 页码（默认 1）|
| `page_size` | int | 每页条数（默认 100，最大 1000）|

### 链路状态

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/chains` | 所有配置链及其状态 |

### 回测管理（仅 Backtest 模式有效）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/backtest/run` | 提交回测任务 |
| GET | `/api/backtest/status/{task_id}` | 查询单个任务状态 |
| GET | `/api/backtest/tasks` | 列出所有任务 |
| GET | `/api/backtest/chains` | 列出可用于回测的链 |

### Pusher 统计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/pusher/stats` | LogPusher 运行统计 |

### 录制与回放

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/replay/recordings` | 列出可用的录制文件 |
| POST | `/api/replay/start` | 启动文件回放（指定文件路径 + 可选区块范围过滤）|

**POST /api/replay/start 参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_path` | string | 是 | 要回放的 JSONL 文件路径 |
| `chain` | string | 否 | 链名称或 chain ID，默认使用第一条配置链 |
| `from_block` | int | 否 | 起始区块过滤 |
| `to_block` | int | 否 | 终止区块过滤 |

### 健康检查

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含链状态、RPC 节点状态、Pusher 信息）|
| GET | `/ready` | 就绪检查 |

### Web 界面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 日志可视化 Web 界面 |

---

## Docker 部署

```bash
# 构建镜像
docker build -t evm-listener .

# 运行容器（实时模式）
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  evm-listener

# 运行容器（回测模式，覆盖启动参数）
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  evm-listener \
  python -m evm_chain_listener.main -c /app/config.yaml -m backtest
```

或使用 Docker Compose：

```bash
docker-compose up -d
```

---

## 项目结构

```
EVMLogListener/
├── src/
│   └── evm_chain_listener/
│       ├── main.py              # 入口文件（CLI + FastAPI 应用 + lifespan）
│       ├── config.py            # 配置加载与解析
│       ├── models.py            # 数据模型（Log, AppConfig, RecorderConfig, ReplayConfig ...）
│       ├── pusher.py            # LogPusher（缓冲式日志推送至 AlertProcessor）
│       ├── recorder.py          # LogRecorder（日志录制至本地 JSONL 文件）
│       ├── replay.py            # LogReplaySource（从本地 JSONL 文件回放日志）
│       ├── templates/
│       │   └── index.html       # Web 前端页面
│       ├── chains/
│       │   ├── __init__.py      # 包初始化
│       │   ├── base.py          # ChainListener（实时模式：增量轮询）
│       │   └── backtest.py      # BacktestRunner（回测模式：批量历史查询）
│       ├── rpc/
│       │   ├── node_pool.py     # RPC 节点池管理
│       │   ├── binary_search.py # 二分查询算法
│       │   └── exceptions.py    # 异常定义
│       ├── cache/
│       │   └── log_cache.py     # FIFO 日志内存缓存
│       └── utils/
│           └── logging.py       # 结构化日志工具
├── docs/
│   └── EVMLISTENER_INPUT_SPEC.md   # AlertProcessor 输入规范文档
├── tests/
│   ├── test_pusher.py          # LogPusher 单元测试（39 个测试用例）
│   └── test_recorder_replay.py # 录制与回放单元测试（17 个测试用例）
├── config.yaml                  # 主配置文件
├── config.yaml.example          # 配置模板
├── requirements.txt             # Python 依赖
├── pytest.ini                   # 测试配置
├── Dockerfile
├── docker-compose.yaml
└── README.md
```

---

## 开发

### 运行测试

```bash
# 确保虚拟环境已激活
pip install pytest pytest-asyncio
pytest tests/
```

当前测试覆盖：
- 原有测试（36 个）：缓存、RPC 池、二分查询等
- LogPusher 测试（39 个）：推送、重试、缓冲管理、回放、统计等
- 录制与回放测试（17 个）：Log.from_dict、LogRecorder、LogReplaySource、完整往返测试

### 代码规范

- Python 类型注解，遵循 PEP 8
- 所有日志输出使用 `extra={"chain", "from_block", "to_block", "log_count", "duration_ms"}` 结构化字段
- 推送至 AlertProcessor 的日志字段 `block_number`/`log_index`/`transaction_index` 均为整数类型（由 `Log.to_push_dict()` 保证）

---

## 许可证

MIT License
