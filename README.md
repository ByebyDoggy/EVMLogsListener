# EVM Chain Listener

一个轻量级的 EVM 兼容区块链事件日志监听器，支持**实时监听（Realtime）**和**历史回测（Backtest）**双模式运行，内置 LogPusher 可将日志推送至 AlertProcessor 进行告警处理，并支持**录制（Record）**与**回放（Replay）**功能。

## 功能特性

- **双模式运行**: 实时模式（增量轮询）+ 回测模式（批量历史查询），共享同一套 RPC 池与缓存
- **多链支持**: 同时监听多条 EVM 兼容链（Ethereum、BSC、Polygon 等）
- **多 RPC 节点池**: 支持配置多个 RPC 节点，按优先级自动故障转移
- **智能查询**: 二分查询算法处理 `eth_getLogs` 结果过大错误
- **速率限制**: 内置请求限流，保护公共/付费 RPC 节点
- **LogPusher**: 缓冲式 HTTP 推送，支持定时触发 + 阈值触发双重策略，自动重试与断线重连
- **录制与回放**: 将日志录制到本地 SQLite 数据库，回测模式下可从数据库回放，无需调用 RPC API
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

### 核心依赖说明

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| `fastapi` | >=0.109.0 | Web 框架，驱动 REST API 和 Swagger 文档 |
| `uvicorn` | >=0.27.0 | ASGI 服务器 |
| `aiohttp` | >=3.9.0 | 异步 HTTP 客户端，用于 RPC 请求 |
| `httpx` | >=0.27.0 | 异步 HTTP 客户端，用于 apipool-server 通信 |
| `apipool-ng` | >=1.0.7 | RPC 密钥池管理，支持本地模式和服务端模式（含 `AsyncDynamicKeyManager`） |
| `pyyaml` | >=6.0 | YAML 配置文件解析 |
| `jinja2` | >=3.1.0 | 模板引擎，用于 Web 界面 |
| `websockets` | >=12.0 | WebSocket 支持 |

> **重要**: `apipool-ng` 是 RPC 节点池的核心依赖，提供异步密钥管理、自动轮换和服务端密钥同步功能。回测模式和实时模式均依赖此库运行。如使用 `apipool_server` 配置（服务端密钥管理模式），还需确保 apipool-server 服务已部署。

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

回测模式用于查询指定区块范围内的历史日志。启动后不会自动执行查询，需通过 **API 提交回测任务** 或 **配置自动启动**。

#### 前置条件

1. 确保 `config.yaml` 中至少配置了一条链的 RPC 节点（`rpc_nodes` 或 `apipool_server`）
2. 回测模式需要 RPC 节点支持 `eth_getLogs` 方法，公共节点可能有查询范围限制
3. 如果使用 `apipool_server` 模式，确保 apipool-server 服务已启动并可访问

#### 启动命令

```bash
cd src

# Linux / macOS
python -m evm_chain_listener.main -c ../config.yaml -m backtest

# Windows（使用虚拟环境）
..\..\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml -m backtest
```

#### 启动后行为

1. 初始化 `LogCache` 内存缓存
2. 检查 `replay` 配置：若配置了 `file_path` 或 `directory`，创建不带 RPC 的 `BacktestRunner`（`use_rpc=False`），从本地文件回放
3. 否则为每条链创建带 RPC 的 `BacktestRunner`（`use_rpc=True`）
4. 如果配置了 `backtest.from_block` / `backtest.to_block`，为每条链自动提交一个回测任务
5. 如果启用了录制（`recorder.enabled: true`），初始化 `LogDbRecorder`
6. 启动 FastAPI 服务，默认监听 `http://0.0.0.0:8080`

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

#### 方式一：通过 API 提交回测任务

默认情况下启动后仅创建 `BacktestRunner` 并等待任务提交，需通过 API 手动发起查询：

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
    "progress_current_block": null,
    "total_batches": 11,
    "completed_batches": 0,
    "logs_found": 0,
    "error": null,
    "started_at": "2026-01-01T12:00:00+00:00",
    "completed_at": null
  }
}
```

> **字段说明**: `started_at` / `completed_at` 使用 ISO 8601 格式（含时区偏移 `+00:00`）。`total_batches` 的计算方式为 `ceil((to_block - from_block + 1) / batch_size)`，例如区块范围 19000000~19001000（共 1001 个区块），`batch_size=1000` 时 `total_batches=2`。

#### 方式二：启动时自动提交回测任务

如果希望启动后立即自动执行回测，无需手动调用 API，可以通过以下方式配置：

**通过 CLI 参数**（优先级最高）：

```bash
# Linux / macOS
python -m evm_chain_listener.main -c ../config.yaml -m backtest --from-block 19000000 --to-block 19002000

# Windows（使用虚拟环境）
..\..\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml -m backtest --from-block 19000000 --to-block 19002000
```

**通过配置文件** `config.yaml` 中的 `backtest` 段：

```yaml
backtest:
  from_block: 19000000          # 起始区块号（含）
  to_block: 19002000            # 终止区块号（含）
  batch_size: 1000              # 每批查询的区块数量，默认 1000
```

> **注意**: CLI `--from-block` / `--to-block` 参数会覆盖配置文件中的 `backtest.from_block` / `backtest.to_block`。设置后端后，会为 `config.yaml` 中**每条已配置的链**分别提交一个回测任务（共享相同的区块范围和 batch_size）。

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

> **过滤器说明**: 回测查询会自动应用 `config.yaml` 中链配置的 `address_filter` 和 `topics_filter`，无需在 API 请求中额外指定。如需修改过滤条件，请更新配置文件后重启服务。

---

### 三、录制模式（Record）

录制模式可在实时模式或回测模式下启用，将捕获的日志自动写入本地 SQLite 数据库，便于后续离线回放。

#### 启用录制

在 `config.yaml` 中配置 `recorder` 段：

```yaml
recorder:
  enabled: true                  # 启用录制
  directory: "./recordings"      # 数据库存储目录（自动创建）
  # db_filename: "logs.db"      # SQLite 文件名（默认 logs.db）
```

#### 工作原理

```
RPC 节点 / 本地文件 ──→ _on_logs_received()
                              │
                   ┌──────────┤──────────┐
                   ▼          ▼          ▼
             LogCache    LogPusher   LogDbRecorder
             （缓存）    （推送）   （写入 SQLite）
                                        │
                              所有链写入同一 .db 文件
                              session 元数据写入 recording_sessions 表
```

#### SQLite 存储优势

1. **高效范围查询**：B-tree 索引 `(chain_id, block_number)` 支持毫秒级按区块范围检索，无需扫描全表
2. **去重保障**：`UNIQUE(chain_id, block_number, transaction_hash, log_index)` 约束自动跳过重复日志
3. **原子写入**：每个批次在单个事务中提交，数据库不会处于部分写入状态
4. **更低磁盘占用**：二进制存储整数和紧凑行格式，通常比文本格式节省 30-50% 空间
5. **流式回放**：使用 `fetchmany()` 分批加载，即使百万级日志也不会内存溢出

#### SQLite 表结构

```sql
-- 日志表：存储所有链的日志记录
CREATE TABLE logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id          INTEGER NOT NULL,
    chain_name        TEXT    NOT NULL,
    block_number      INTEGER NOT NULL,
    block_hash        TEXT    NOT NULL,
    transaction_hash  TEXT    NOT NULL,
    transaction_index INTEGER NOT NULL,
    log_index         INTEGER NOT NULL,
    address           TEXT    NOT NULL,
    data              TEXT,
    topics            TEXT,       -- JSON 数组存储为文本
    removed           INTEGER NOT NULL DEFAULT 0,
    timestamp         TEXT,       -- ISO 8601 字符串
    UNIQUE(chain_id, block_number, transaction_hash, log_index)
);

-- 索引：加速按链+区块范围查询
CREATE INDEX idx_logs_chain_block ON logs (chain_id, block_number);

-- 会话表：记录每次录制的元信息
CREATE TABLE recording_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_name  TEXT    NOT NULL,
    chain_id    INTEGER NOT NULL,
    from_block  INTEGER,
    to_block    INTEGER,
    total_logs  INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT    NOT NULL,
    closed_at   TEXT
);
```

#### 录制文件格式

- **数据库文件**: `<directory>/<db_filename>`（默认 `./recordings/logs.db`）
- 所有链的日志写入同一个数据库文件
- 会话元信息存储在 `recording_sessions` 表中

#### JSONL → SQLite 数据迁移

如果已有旧版 JSONL 格式的录制文件，可以使用迁移工具转换为 SQLite：

```bash
# 迁移目录下所有 JSONL 文件
python scripts/migrate_jsonl_to_sqlite.py ./recordings

# 指定输出数据库路径
python scripts/migrate_jsonl_to_sqlite.py ./recordings --output ./recordings/logs.db

# 调整批量插入大小（默认 5000）
python scripts/migrate_jsonl_to_sqlite.py ./recordings --batch-size 10000

# 迁移单个文件
python scripts/migrate_jsonl_to_sqlite.py ./recordings/ethereum_20260411.jsonl
```

---

### 四、回放模式（Replay）

回放模式在回测（backtest）模式下使用，从本地 SQLite 数据库加载日志，**无需调用 RPC API**，适合离线分析、重复回测等场景。

#### 配置回放

在 `config.yaml` 中配置 `replay` 段：

```yaml
# 方式一：从指定数据库回放
replay:
  file_path: "./recordings/logs.db"
  from_block: 19000000   # 可选：过滤起始区块
  to_block: 19001000     # 可选：过滤终止区块

# 方式二：指定目录（自动发现 .db 文件）
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
2. 从指定数据库加载日志
3. 使用 `fetchmany()` 流式加载，按 `batch_size` 分批读取，内存占用恒定
4. 通过 `_row_to_log()` 反序列化，确保数据结构一致
5. 将日志注入缓存和 Pusher，与正常运行流程完全相同

#### SQLite 回放的内存控制

SQLite 回放模式专为大规模数据设计：

- **索引查询**：使用 `(chain_id, block_number)` 索引，只检索指定区块范围内的数据
- **流式加载**：通过 `fetchmany(batch_size)` 分批获取结果，不会将全部数据加载到内存
- **只读模式**：以 `?mode=ro` URI 参数打开数据库，避免意外修改
- **百万级支持**：即使数据库中有数百万条日志，回放时内存占用仍保持在 `batch_size` 级别

```
                    SQLite 数据库
                    ┌──────────────┐
                    │  logs 表      │  ← B-tree 索引 (chain_id, block_number)
                    │  1,000,000+  │
                    │  行日志数据    │
                    └──────┬───────┘
                           │
                WHERE chain_id=? AND block_number BETWEEN ? AND ?
                           │
                           ▼
                   ┌──────────────┐
                   │ fetchmany()  │  ← 每次只取 batch_size 行
                   │ batch_size   │
                   │ = 1000       │
                   └──────┬───────┘
                          │ 循环
                          ▼
                   log_callback(batch)
```

#### 通过 API 管理回放

```bash
# 列出可用录制数据库
curl "http://localhost:8080/api/replay/recordings"

# 从 SQLite 数据库回放
curl -X POST "http://localhost:8080/api/replay/start?file_path=./recordings/logs.db&chain=ethereum"

# 带区块范围过滤的回放（SQLite 高效索引查询）
curl -X POST "http://localhost:8080/api/replay/start?file_path=./recordings/logs.db&from_block=19000000&to_block=19001000"
```

#### 数据一致性保证

录制与回放使用相同的数据模型（`Log`），确保完整往返一致性：

| 步骤 | SQLite 模式 |
|------|-------------|
| 录制 | 整数存储 + JSON 序列化 topics |
| 回放 | `_row_to_log()` 直接构造 `Log` |
| 去重 | `INSERT OR IGNORE` 数据库级去重 |

---

## 配置项详解

### 完整配置文件结构

```yaml
chains:                          # 链路配置（数组，支持多条链）
  - name: ethereum               # 链名称（必填，用于标识和 API 引用）
    chain_id: 1                  # 链 ID（必填，整数）
    poll_interval: 30            # 实时模式：轮询间隔（秒），仅 realtime 模式生效
    address_filter:              # 可选：合约地址过滤（回测和实时模式均生效）
      - "0x1234..."             # 仅查询这些合约地址的日志
    topics_filter:               # 可选：事件 topic 过滤（回测和实时模式均生效）
      - "0xabc..."              # 仅查询匹配这些 topic 的日志
    # --- RPC 节点配置（二选一）---
    # 方式一：本地 RPC 节点列表
    rpc_nodes:                   # RPC 节点列表（至少配置 1 个，或使用 apipool_server）
      - url: "https://..."       # RPC 节点 URL（必填，支持 ${ENV_VAR} 环境变量替换）
        priority: 1              # 优先级（数字越小越优先），默认 100
        type: public             # 节点类型：public / paid，用于标记
        rate_limit:              # 速率限制配置
          requests_per_second: 5 # 每秒请求数
          burst: 10              # 突发请求数
    # 方式二：apipool-server 自动加载（v1.0.5+，与 rpc_nodes 二选一）
    apipool_server:              # 从 apipool-server 动态获取 RPC 节点列表
      service_url: "http://localhost:8000"  # apipool-server 地址
      pool_identifier: "ethereum-rpc"       # 服务端池标识符
      username: "user1"                     # 登录用户名
      password: "12345678"                  # 登录密码（支持 ${ENV_VAR}）

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
  directory: "./recordings"      # 数据库存储目录（自动创建）
  # db_filename: "logs.db"      # SQLite 文件名（默认 logs.db）

replay:                          # 日志回放配置（仅 backtest 模式生效）
  file_path: null                # 单文件模式：指定 SQLite 数据库路径
  directory: null                # 目录模式：自动发现目录下的 .db 文件（与 file_path 二选一）
  from_block: null               # 可选：过滤起始区块号
  to_block: null                 # 可选：过滤终止区块号

backtest:                        # 回测启动配置（仅 backtest 模式生效）
  from_block: null               # 启动时自动提交回测任务的起始区块号（含），需与 to_block 同时设置
  to_block: null                 # 启动时自动提交回测任务的终止区块号（含），需与 from_block 同时设置
  batch_size: 1000               # 每批查询的区块数量，默认 1000
                                 # 注意：设置后端后，会为 config.yaml 中每条已配置的链分别提交一个回测任务
```

### 配置项速查表

| 配置路径 | 说明 | 默认值 | 必填 |
|----------|------|--------|------|
| `chains[].name` | 链名称 | — | 是 |
| `chains[].chain_id` | 链 ID | — | 是 |
| `chains[].poll_interval` | 实时模式轮询间隔（秒）| 30 | 否 |
| `chains[].address_filter` | 合约地址过滤列表 | — | 否 |
| `chains[].topics_filter` | 事件 topic 过滤列表 | — | 否 |
| `chains[].rpc_nodes[].url` | RPC URL | — | 与 apipool_server 二选一 |
| `chains[].rpc_nodes[].priority` | 节点优先级 | 100 | 否 |
| `chains[].rpc_nodes[].type` | 节点类型 | public | 否 |
| `chains[].rpc_nodes[].rate_limit.requests_per_second` | QPS 限制 | 5 | 否 |
| `chains[].apipool_server.service_url` | apipool-server 地址 | — | 与 rpc_nodes 二选一 |
| `chains[].apipool_server.pool_identifier` | 服务端池标识符 | — | 启用时必填 |
| `chains[].apipool_server.username` | 登录用户名 | — | 启用时必填 |
| `chains[].apipool_server.password` | 登录密码 | — | 启用时必填 |
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
| `recorder.directory` | 录制数据库存储目录 | ./recordings | 否 |
| `recorder.db_filename` | SQLite 文件名 | logs.db | 否 |
| `replay.file_path` | 回放 SQLite 数据库路径 | — | 否 |
| `replay.directory` | 回放目录（自动发现） | — | 否 |
| `replay.from_block` | 回放起始区块过滤 | — | 否 |
| `replay.to_block` | 回放终止区块过滤 | — | 否 |
| `backtest.from_block` | 启动时自动回测起始区块 | — | 否 |
| `backtest.to_block` | 启动时自动回测终止区块 | — | 否 |
| `backtest.batch_size` | 回测每批查询区块数 | 1000 | 否 |

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
usage: python -m evm_chain_listener.main [-h] [-c CONFIG] [-m {realtime,backtest}] [-v] [--host HOST] [--port PORT] [--from-block FROM_BLOCK] [--to-block TO_BLOCK]

options:
  -c, --config         配置文件路径（默认: config.yaml）
  -m, --mode           运行模式: realtime（实时监听）或 backtest（历史回测）（默认: realtime）
  --host               覆盖 API 监听地址
  --port               覆盖 API 监听端口
  --from-block         回测起始区块号（覆盖配置文件 backtest.from_block）
  --to-block           回测终止区块号（覆盖配置文件 backtest.to_block）
  -v, --version        显示版本号
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
| `file_path` | string | 是 | 要回放的 SQLite 数据库文件路径 |
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
│       ├── db_recorder.py       # LogDbRecorder（日志录制至本地 SQLite 数据库）
│       ├── db_replay.py         # LogDbReplaySource（从 SQLite 数据库回放日志）
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
- 录制与回放测试：Log.from_dict、LogDbRecorder、LogDbReplaySource、完整往返测试

### 代码规范

- Python 类型注解，遵循 PEP 8
- 所有日志输出使用 `extra={"chain", "from_block", "to_block", "log_count", "duration_ms"}` 结构化字段
- 推送至 AlertProcessor 的日志字段 `block_number`/`log_index`/`transaction_index` 均为整数类型（由 `Log.to_push_dict()` 保证）

---

## 许可证

MIT License
