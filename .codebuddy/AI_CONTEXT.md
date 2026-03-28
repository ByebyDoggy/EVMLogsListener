# EVM Chain Listener - AI Context

本文档为 AI 助手提供项目上下文，帮助快速理解项目结构和代码修改建议。

## 项目概述

EVM Chain Listener 是一个异步的 EVM 兼容区块链事件日志监听器。

**核心功能**:
- 多链并发监听
- 多 RPC 节点故障转移
- 二分查询处理大结果集
- 内存 FIFO 缓存
- REST API 查询接口

**技术栈**: Python 3.10+, asyncio, aiohttp, PyYAML

## 快速导航

### 入口点
- `src/evm_chain_listener/main.py` - 主入口，`main()` 函数和 `Application` 类

### 核心模块
| 模块 | 文件 | 职责 |
|------|------|------|
| 配置加载 | `config.py` | YAML 解析、环境变量替换、配置验证 |
| 数据模型 | `models.py` | Log, ChainConfig, RPCNodeConfig 等数据类 |
| 链监听 | `chains/base.py` | ChainListener 类，轮询逻辑 |
| RPC 交互 | `rpc/node_pool.py` | RPCNode, RPCNodePool，节点管理 |
| 二分查询 | `rpc/binary_search.py` | BinarySearchQuerier，处理 -32005 错误 |
| 日志缓存 | `cache/log_cache.py` | LogCache，FIFO 缓存 |
| API 处理 | `api/handlers.py` | HTTP 端点实现 |

## 关键数据结构

### Log (models.py)
```python
@dataclass
class Log:
    address: str
    topics: List[str]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int
    transaction_index: int
    block_hash: str
    removed: bool = False
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    timestamp: Optional[datetime] = None
```

### ChainConfig (models.py)
```python
@dataclass
class ChainConfig:
    name: str
    chain_id: int
    poll_interval: int = 30
    rpc_nodes: List[RPCNodeConfig] = field(default_factory=list)
    address_filter: Optional[List[str]] = None
    topics_filter: Optional[List[str]] = None
```

## 核心流程

### 应用启动流程
```
main() 
  -> asyncio.run(async_main())
    -> Application(config_path)
      -> load_config()
      -> LogCache()
    -> app.run()
      -> _create_listeners()
      -> listener.start() (创建 asyncio.Task)
      -> _start_api_server()
      -> 等待 stop_event
```

### 单次轮询流程 (ChainListener._poll)
```
1. rpc_pool.get_block_number() -> current_block
2. 如果 _last_block 为空，初始化并返回
3. 计算 from_block = _last_block + 1, to_block = current_block
4. querier.query_logs(from_block, to_block)
5. 更新 _last_block = to_block
6. log_callback(logs) -> cache.add_many(logs)
```

### RPC 节点选择流程
```
1. 获取所有健康节点（按 priority 排序）
2. 如果没有健康节点，使用所有节点
3. 遍历节点尝试请求
4. 成功则返回
5. 失败则标记不健康，尝试下一节点
6. 全部失败则抛出 AllNodesFailedError
```

### 二分查询流程 (BinarySearchQuerier._do_query)
```
1. 尝试 rpc_pool.get_logs()
2. 成功则返回
3. 捕获 LogLimitExceededError
4. 如果 from_block == to_block，返回空列表
5. 计算 mid = (from + to) // 2
6. 并行递归查询 [from_block, mid] 和 [mid+1, to_block]
7. 合并结果返回
```

## 重要实现细节

### Windows 兼容性
- `asyncio.add_signal_handler()` 在 Windows 上不可用
- `main.py` 中使用 try/except NotImplementedError 处理
- 使用 `signal.signal()` 作为 Windows 回退方案

### 速率限制
- 每个 RPCNode 维护 `_last_request_time`
- 根据 `rate_limit.requests_per_second` 计算最小间隔
- 请求前 sleep 确保不超过速率

### 节点健康追踪
- `_is_healthy`: 当前健康状态
- `_consecutive_failures`: 连续失败次数
- 连续 3 次失败后标记为不健康
- 成功请求后立即恢复健康

### 缓存去重
- 使用 `log.unique_key` = `f"{transaction_hash}:{log_index}"`
- `_index` 字典用于 O(1) 去重检查

## 常见修改场景

### 添加新的过滤条件
1. 在 `ChainConfig` 添加字段 (models.py)
2. 在 `config.py` 的 `parse_chain_config()` 解析配置
3. 在 `ChainListener._poll()` 使用新过滤条件

### 添加新的 API 端点
1. 在 `APIHandler` 添加异步方法 (api/handlers.py)
2. 在 `create_app()` 注册路由 (api/routes.py)

### 添加新的 RPC 方法
1. 在 `RPCNode` 添加方法 (rpc/node_pool.py)
2. 使用 `self.request(method, params)` 发送请求

### 修改缓存策略
1. 修改 `LogCache` 类 (cache/log_cache.py)
2. 注意线程安全，使用 `self._lock`

## 配置示例

```yaml
chains:
  - name: ethereum
    chain_id: 1
    poll_interval: 30
    rpc_nodes:
      - url: "https://ethereum-rpc.publicnode.com"
        priority: 1
        type: public
        rate_limit:
          requests_per_second: 5
          burst: 10

cache:
  max_size: 10000

api:
  host: "0.0.0.0"
  port: 8080

log:
  level: INFO
  format: json
  output: stdout
```

## 测试文件位置

- `tests/` 目录
- 使用 pytest + pytest-asyncio

## Docker 相关

- `Dockerfile`: 构建镜像
- `docker-compose.yaml`: 编排配置
- 默认暴露 8080 端口

## 注意事项

1. **异步编程**: 所有 I/O 操作都是异步的，使用 `async/await`
2. **线程安全**: LogCache 使用 `threading.RLock`，其他组件依赖 asyncio 单线程模型
3. **错误处理**: 区分可重试错误和不可重试错误
4. **资源清理**: 确保在关闭时关闭 aiohttp 会话
