# 接口定义

## 配置文件接口

### 主配置文件 (config.yaml)

```yaml
chains:
  - name: string           # 链名称标识
    chain_id: integer     # EIP-155 chain ID
    poll_interval: integer # 轮询间隔（秒）
    rpc_nodes:
      - url: string        # RPC节点URL，支持环境变量${VAR}格式
        priority: integer  # 优先级，数字越小优先级越高
        type: string       # 节点类型: "public" | "paid" | "alchemy" | "infura"
    rate_limit:
      requests_per_second: integer  # 该节点每秒最大请求数
      burst: integer                # 突发容量

webhook:
  endpoints:
    - url: string         # Webhook接收URL
      timeout: integer    # 请求超时（秒），默认10
      retry_times: integer # 重试次数，默认3
      retry_delay: integer # 重试间隔（秒），默认5
      headers:             # 自定义请求头
        Authorization: string

log:
  level: string           # 日志级别: DEBUG | INFO | WARNING | ERROR
  format: string          # 日志格式: json | text
  output: string          # 输出目标: stdout | file
  file_path: string       # 当output为file时的路径

health:
  enabled: boolean        # 是否启用健康检查
  port: integer           # 健康检查端口
  endpoint: string        # 健康检查路径
```

### 环境变量

| 变量名 | 描述 | 示例 |
|--------|------|------|
| `ALCHEMY_ETH_URL` | Alchemy Ethereum RPC URL | `https://eth-mainnet.g.alchemy.com/v2/xxx` |
| `INFURA_ETH_URL` | Infura Ethereum RPC URL | `https://mainnet.infura.io/v3/xxx` |
| `BSC_RPC_URL` | BSC RPC URL | `https://bsc-dataseed.binance.org` |
| `POLYGON_RPC_URL` | Polygon RPC URL | `https://polygon-rpc.com` |
| `WEBHOOK_SECRET` | Webhook签名密钥 | `your-secret-key` |

## Webhook接口

### 日志事件推送

**请求**

```http
POST /events HTTP/1.1
Host: your-webhook.com
Content-Type: application/json
X-Signature: sha256=xxx  # 可选，HMAC签名
X-Chain-ID: 1
X-Request-ID: uuid

{
  "version": "1.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "chain": {
    "name": "ethereum",
    "chain_id": 1
  },
  "logs": [
    {
      "log_index": "0x1a",
      "transaction_hash": "0xabc123...",
      "transaction_index": "0x5",
      "block_number": "0x12345678",
      "block_hash": "0xdef456...",
      "address": "0xContractAddress",
      "data": "0x...",
      "topics": [
        "0xEventSignatureHash",
        "0xArg1",
        "0xArg2"
      ],
      "removed": false
    }
  ],
  "from_block": "0x12345670",
  "to_block": "0x12345678",
  "count": 15
}
```

**响应**

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "status": "success",
  "request_id": "uuid"
}
```

### 错误推送

**请求**

```http
POST /events HTTP/1.1
Content-Type: application/json
X-Event-Type: error

{
  "version": "1.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "event_type": "error",
  "chain": {
    "name": "ethereum",
    "chain_id": 1
  },
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "RPC node rate limit exceeded",
    "details": {
      "node_url": "https://eth.public-rpc.com",
      "retry_after": 5
    }
  }
}
```

## 健康检查接口

### GET /health

**响应**

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime": 3600,
  "chains": {
    "ethereum": {
      "status": "running",
      "last_block": 19000000,
      "last_poll": "2024-01-15T10:29:55Z"
    },
    "bsc": {
      "status": "running",
      "last_block": 35000000,
      "last_poll": "2024-01-15T10:29:58Z"
    }
  },
  "rpc_nodes": {
    "ethereum": {
      "active": "infura",
      "available": ["alchemy", "infura"],
      "failed": ["public-rpc"]
    }
  }
}
```

### GET /ready

**响应**

```json
{
  "ready": true,
  "checks": {
    "config_loaded": true,
    "rpc_nodes_configured": true,
    "webhook_configured": true
  }
}
```

## 内部模块接口

### RPCNodePool

```python
class RPCNodePool:
    def __init__(self, nodes: List[RPCNodeConfig]):
        ...

    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None
    ) -> List[Log]:
        """获取指定区间的日志"""
        ...

    async def health_check(self) -> NodeHealthStatus:
        """检查所有节点的健康状态"""
        ...
```

### BinarySearchQuerier

```python
class BinarySearchQuerier:
    def __init__(
        self,
        rpc_pool: RPCNodePool,
        max_results_per_query: int = 10000
    ):
        ...

    async def query_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None
    ) -> List[Log]:
        """
        使用二分法查询日志
        当区间日志数超过限制时自动拆分
        """
        ...
```

### ChainListener

```python
class ChainListener:
    def __init__(
        self,
        name: str,
        chain_id: int,
        rpc_pool: RPCNodePool,
        poll_interval: int,
        event_callback: Callable[[List[Log]], None]
    ):
        ...

    async def start(self) -> None:
        """启动监听"""
        ...

    async def stop(self) -> None:
        """停止监听"""
        ...

    def get_status(self) -> ListenerStatus:
        """获取监听状态"""
        ...
```

## 数据结构

### Log

```python
@dataclass
class Log:
    address: str           # 合约地址
    topics: List[str]      # 事件主题
    data: str              # 原始数据
    block_number: int      # 区块号
    transaction_hash: str  # 交易哈希
    log_index: int         # 日志索引
    transaction_index: int # 交易索引
    block_hash: str        # 区块哈希
    removed: bool          # 是否被移除（log删除）
```

### RPCNodeConfig

```python
@dataclass
class RPCNodeConfig:
    url: str
    priority: int          # 优先级
    node_type: str         # 节点类型
    rate_limit: RateLimit  # 速率限制配置

@dataclass
class RateLimit:
    requests_per_second: int
    burst: int
```

### ChainConfig

```python
@dataclass
class ChainConfig:
    name: str
    chain_id: int
    poll_interval: int
    rpc_nodes: List[RPCNodeConfig]
    filters: Optional[ChainFilters] = None
```
