# 架构设计文档

## 概述

EVM Chain Listener 是一个异步事件驱动架构的区块链日志监听系统，采用分层模块化设计。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Application Layer                       │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    main.py                           │   │
│  │  - Application 类: 应用生命周期管理                   │   │
│  │  - 信号处理: 优雅关闭                                │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼─────────────────────────────┐
│                     Core Layer                              │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  │
│  │  ChainListener │  │   LogCache   │  │   APIHandler  │  │
│  │  (chains/base) │  │(cache/log_   │  │  (api/handlers)│  │
│  │               │  │    cache.py) │  │               │  │
│  │ - 轮询区块    │  │ - FIFO 存储  │  │ - REST 接口   │  │
│  │ - 获取日志    │──│ - 去重索引   │──│ - 分页查询    │  │
│  │ - 状态追踪    │  │ - 统计信息   │  │ - 健康检查    │  │
│  └───────┬───────┘  └───────────────┘  └───────────────┘  │
└──────────┼─────────────────────────────────────────────────┘
           │
┌──────────┼─────────────────────────────────────────────────┐
│          │           RPC Layer                              │
│  ┌───────▼───────┐  ┌───────────────┐  ┌───────────────┐  │
│  │  RPCNodePool  │  │BinarySearch   │  │   RPCNode     │  │
│  │ (rpc/node_    │  │Querier        │  │               │  │
│  │    pool.py)   │  │(rpc/binary_   │  │ - HTTP 会话   │  │
│  │               │  │  search.py)   │  │ - 速率限制    │  │
│  │ - 节点选择    │──│               │──│ - 健康追踪    │  │
│  │ - 故障转移    │  │ - 二分分割    │  │ - 错误处理    │  │
│  │ - 健康检查    │  │ - 自动重试    │  │               │  │
│  └───────────────┘  └───────────────┘  └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼─────────────────────────────┐
│                   Infrastructure Layer                      │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  │
│  │    Config     │  │    Models     │  │    Logging    │  │
│  │  (config.py)  │  │  (models.py)  │  │(utils/logging)│  │
│  │               │  │               │  │               │  │
│  │ - YAML 解析   │  │ - Log        │  │ - JSON 格式   │  │
│  │ - 环境变量    │  │ - ChainConfig │  │ - 结构化日志  │  │
│  │ - 验证       │  │ - RPCConfig   │  │               │  │
│  └───────────────┘  └───────────────┘  └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. Application (main.py)

应用主入口，负责：
- 初始化所有组件
- 管理应用生命周期
- 处理系统信号（SIGINT/SIGTERM）
- 协调链监听器和 API 服务

```python
class Application:
    def __init__(self, config_path: str)
    async def run(self) -> None
    async def _start(self) -> None
    async def _stop(self) -> None
```

### 2. ChainListener (chains/base.py)

单链监听器，核心逻辑：

```python
class ChainListener:
    async def start(self) -> None      # 启动监听
    async def stop(self) -> None       # 停止监听
    async def _poll_loop(self) -> None # 主轮询循环
    async def _poll(self) -> None      # 单次轮询
```

**轮询流程**:
1. 获取当前区块高度 (`eth_blockNumber`)
2. 首次运行：记录当前区块，等待下一轮
3. 后续运行：查询新区块的日志 (`eth_getLogs`)
4. 更新最后处理的区块号
5. 将日志存入缓存

### 3. RPCNodePool (rpc/node_pool.py)

RPC 节点池管理器：

```python
class RPCNodePool:
    async def get_logs(...) -> List[Log]
    async def get_block_number() -> int
    async def health_check() -> NodeHealthStatus
```

**节点选择策略**:
- 按 priority 排序
- 优先使用健康节点
- 自动标记不健康节点（连续 3 次失败）
- 故障时自动切换到下一节点

### 4. BinarySearchQuerier (rpc/binary_search.py)

二分查询优化器，解决 `eth_getLogs` 结果过大问题：

```python
class BinarySearchQuerier:
    async def query_logs(...) -> List[Log]
```

**工作原理**:
1. 尝试查询指定区块范围
2. 如果返回 `LogLimitExceededError` (-32005)
3. 将区块范围二分，递归查询
4. 合并所有结果返回

### 5. LogCache (cache/log_cache.py)

线程安全的内存缓存：

```python
class LogCache:
    def add(self, log: Log) -> bool
    def add_many(self, logs: List[Log]) -> int
    def query(self, ...) -> PaginatedResult
    def get_stats(self) -> CacheStats
```

**特性**:
- FIFO 淘汰策略
- 基于 `transaction_hash:log_index` 去重
- 支持多条件过滤查询
- 线程安全（使用 RLock）

### 6. APIHandler (api/handlers.py)

HTTP API 处理器：

```python
class APIHandler:
    async def handle_logs(self, request) -> Response
    async def handle_logs_stats(self, request) -> Response
    async def handle_health(self, request) -> Response
    async def handle_ready(self, request) -> Response
```

## 数据流

### 日志采集流程

```
┌─────────┐    ┌─────────────────┐    ┌──────────────┐
│  RPC    │───▶│  ChainListener  │───▶│   LogCache   │
│  Node   │    │  (poll_loop)    │    │  (add_many)  │
└─────────┘    └─────────────────┘    └──────────────┘
     │                │                      │
     │ eth_getLogs    │ log_callback         │
     │                ▼                      │
     │        ┌──────────────┐               │
     │        │BinarySearch  │               │
     │        │  Querier     │               │
     │        └──────────────┘               │
     │                                       ▼
     │                               ┌──────────────┐
     │                               │  APIHandler  │
     └──────────────────────────────▶│  (query)     │
                                     └──────────────┘
```

### 故障转移流程

```
┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│   Node A      │     │   Node B      │     │   Node C      │
│  (priority=1) │     │  (priority=2) │     │  (priority=3) │
└───────┬───────┘     └───────┬───────┘     └───────┬───────┘
        │                     │                     │
        │ Success             │                     │
        ▼                     │                     │
   ┌─────────┐                │                     │
   │ Return  │                │                     │
   │ Result  │                │                     │
   └─────────┘                │                     │
                              │                     │
        ──────────────────────┼─────────────────────┘
        │                     │                     │
        │ Failure             │                     │
        ▼                     │                     │
   mark_unhealthy()           │                     │
        │                     │                     │
        └────────────────────▶│                     │
                              │                     │
                              │ Success             │
                              ▼                     │
                         ┌─────────┐                │
                         │ Return  │                │
                         │ Result  │                │
                         └─────────┘                │
                                                    │
                              ──────────────────────┘
```

## 并发模型

- 使用 `asyncio` 实现异步 I/O
- 每条链一个独立的轮询任务 (`asyncio.Task`)
- HTTP API 使用 `aiohttp` 处理并发请求
- 缓存使用 `threading.RLock` 保证线程安全

## 错误处理

### 异常层次

```
RPCError (base)
├── RateLimitError (429)
├── LogLimitExceededError (-32005)
├── ConnectionError
├── TimeoutError
├── InvalidResponseError
└── AllNodesFailedError
```

### 重试策略

- 单节点失败：切换到下一节点
- 所有节点失败：等待 `poll_interval` 后重试
- 查询失败：指数退避重试（最多 3 次）

## 扩展点

### 添加新链

在 `config.yaml` 中添加链配置：

```yaml
chains:
  - name: arbitrum
    chain_id: 42161
    poll_interval: 10
    rpc_nodes:
      - url: "https://arb1.arbitrum.io/rpc"
        priority: 1
```

### 自定义日志处理器

修改 `Application._on_logs_received` 方法：

```python
def _on_logs_received(self, logs: List[Log]) -> None:
    # 自定义处理逻辑
    for log in logs:
        # 发送到消息队列、写入数据库等
        pass
    self._cache.add_many(logs)
```

### 添加 API 端点

1. 在 `api/handlers.py` 添加处理方法
2. 在 `api/routes.py` 注册路由
