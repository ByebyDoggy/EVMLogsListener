# 开发者指南

## 项目结构

```
evm-chain-listener/
├── src/
│   └── evm_chain_listener/
│       ├── __init__.py
│       ├── main.py              # 程序入口
│       ├── config.py            # 配置加载
│       ├── models.py             # 数据模型
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py         # HTTP路由
│       │   └── handlers.py       # 请求处理器
│       ├── cache/
│       │   ├── __init__.py
│       │   └── log_cache.py      # 日志缓存
│       ├── chains/
│       │   ├── __init__.py
│       │   ├── base.py           # 链监听基类
│       │   ├── ethereum.py       # Ethereum实现
│       │   ├── bsc.py            # BSC实现
│       │   └── polygon.py        # Polygon实现
│       ├── rpc/
│       │   ├── __init__.py
│       │   ├── node_pool.py      # RPC节点池
│       │   ├── binary_search.py   # 二分法查询
│       │   └── exceptions.py      # RPC异常
│       └── utils/
│           ├── __init__.py
│           └── logging.py        # 日志工具
├── config.yaml.example           # 配置示例
├── Dockerfile
├── docker-compose.yaml
├── requirements.txt
└── README.md
```

## 环境要求

- Python 3.10+
- Docker & Docker Compose (用于部署)

## 本地开发

### 1. 克隆代码

```bash
git clone <repository-url>
cd evm-chain-listener
```

### 2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate  # Windows
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml 填入你的RPC节点URL和Webhook地址
```

### 5. 运行

```bash
python -m src.evm_chain_listener.main
```

## 配置说明

### config.yaml 完整配置

```yaml
chains:
  - name: ethereum
    chain_id: 1
    poll_interval: 30
    rpc_nodes:
      - url: "${ALCHEMY_ETH_URL}"
        priority: 1
        type: alchemy
        rate_limit:
          requests_per_second: 10
          burst: 20
      - url: "${INFURA_ETH_URL}"
        priority: 2
        type: infura
        rate_limit:
          requests_per_second: 10
          burst: 20
      - url: "https://eth.public-rpc.com"
        priority: 3
        type: public
        rate_limit:
          requests_per_second: 1
          burst: 5

cache:
  max_size: 10000

api:
  host: "0.0.0.0"
  port: 8080

log:
  level: INFO
  format: json
  output: stdout

health:
  enabled: true
  endpoint: /health
```

### 环境变量

创建 `.env` 文件或导出环境变量：

```bash
export ALCHEMY_ETH_URL="https://eth-mainnet.g.alchemy.com/v2/your-api-key"
export INFURA_ETH_URL="https://mainnet.infura.io/v3/your-api-key"
export BSC_RPC_URL="https://bsc-dataseed.binance.org"
export POLYGON_RPC_URL="https://polygon-rpc.com"
```

## Docker 部署

### 构建镜像

```bash
docker build -t evm-chain-listener:latest .
```

### 使用 docker-compose

```yaml
version: '3.8'
services:
  evm-listener:
    image: evm-chain-listener:latest
    environment:
      - ALCHEMY_ETH_URL=${ALCHEMY_ETH_URL}
      - INFURA_ETH_URL=${INFURA_ETH_URL}
      - BSC_RPC_URL=${BSC_RPC_URL}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    ports:
      - "8080:8080"
    restart: unless-stopped
```

```bash
docker-compose up -d
```

## 核心模块开发

### Log Cache (cache/log_cache.py)

内存缓存，负责存储和管理日志：

```python
class LogCache:
    def __init__(self, max_size: int = 10000):
        self._cache: deque = deque(maxlen=max_size)
        self._index: Dict[str, Log] = {}
    
    def add(self, log: Log) -> None:
        key = f"{log.transaction_hash}:{log.log_index}"
        if key not in self._index:
            self._cache.append(log)
            self._index[key] = log
    
    def query(
        self,
        chain_id: Optional[int] = None,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
        page: int = 1,
        page_size: int = 100
    ) -> PaginatedResult:
        filtered = self._cache
        if chain_id is not None:
            filtered = [l for l in filtered if l.chain_id == chain_id]
        if from_block is not None:
            filtered = [l for l in filtered if l.block_number >= from_block]
        if to_block is not None:
            filtered = [l for l in filtered if l.block_number <= to_block]
        # 分页
        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        return PaginatedResult(
            items=filtered[start:end],
            total=total,
            page=page,
            page_size=page_size
        )
```

### RPC节点池 (rpc/node_pool.py)

节点池负责管理多个RPC节点的访问：

```python
class RPCNodePool:
    def __init__(self, nodes: List[RPCNodeConfig]):
        self._nodes = sorted(nodes, key=lambda n: n.priority)
        self._current_index = 0
        self._health_status = {n.url: True for n in nodes}

    async def get_logs(self, from_block: int, to_block: int, 
                       address: Optional[str] = None,
                       topics: Optional[List[str]] = None) -> List[Log]:
        # 遍历所有可用节点直到成功
        for node in self._get_available_nodes():
            try:
                return await node.eth_get_logs(from_block, to_block, 
                                               address, topics)
            except RateLimitError:
                self._mark_unhealthy(node.url)
                continue
        raise AllNodesFailedError()
```

### 二分法查询 (rpc/binary_search.py)

当日志量超限时自动拆分查询：

```python
class BinarySearchQuerier:
    MAX_LOGS_PER_QUERY = 10000  # 大多数RPC节点限制

    async def query_logs(self, from_block: int, to_block: int, 
                         address: Optional[str] = None,
                         topics: Optional[List[str]] = None) -> List[Log]:
        try:
            return await self._rpc_pool.get_logs(
                from_block, to_block, address, topics)
        except LogLimitExceededError:
            if from_block == to_block:
                # 单区块仍超限，返回空或记录错误
                return []
            mid = (from_block + to_block) // 2
            left_logs = await self.query_logs(
                from_block, mid, address, topics)
            right_logs = await self.query_logs(
                mid + 1, to_block, address, topics)
            return left_logs + right_logs
```

### 链监听器 (chains/base.py)

```python
class ChainListener:
    def __init__(self, name: str, chain_id: int,
                 rpc_pool: RPCNodePool,
                 poll_interval: int,
                 event_callback: Callable):
        self.name = name
        self.chain_id = chain_id
        self.rpc_pool = rpc_pool
        self.poll_interval = poll_interval
        self.event_callback = event_callback
        self._last_block = None
        self._running = False

    async def _poll(self):
        while self._running:
            try:
                current_block = await self.rpc_pool.get_block_number()
                if self._last_block is None:
                    self._last_block = current_block - 1
                
                if current_block > self._last_block:
                    logs = await self.binary_search_querier.query_logs(
                        self._last_block + 1, current_block)
                    if logs:
                        await self.event_callback(logs)
                    self._last_block = current_block
            except Exception as e:
                logger.error(f"Poll error on {self.name}: {e}")
            
            await asyncio.sleep(self.poll_interval)
```

## 测试

### 运行测试

```bash
pytest tests/ -v
```

### 测试覆盖

- RPC节点池故障切换
- 二分法查询拆分逻辑
- 日志缓存FIFO淘汰
- 配置加载

## 日志格式

JSON格式日志示例：

```json
{
  "timestamp": "2024-01-15T10:30:00.123Z",
  "level": "INFO",
  "message": "Polling logs",
  "chain": "ethereum",
  "from_block": 19000000,
  "to_block": 19000010,
  "log_count": 25,
  "duration_ms": 150
}
```

## 常见问题

### Q: RPC节点返回429错误怎么办？

A: 节点池会自动切换到下一个节点，并使用指数退避策略等待后重试当前请求。

### Q: 日志量太大查询失败怎么办？

A: 二分法查询器会自动检测`Log filter too large`错误，将大区间拆分为小区间逐个查询。

### Q: 如何添加新的EVM兼容链？

A: 在`chains/`目录下创建新的链实现类，继承`ChainListener`基类，然后在`main.py`中注册。

### Q: 缓存满了怎么办？

A: 缓存使用FIFO策略，自动清除最旧的日志。可以通过配置`cache.max_size`调整缓存大小。

### Q: 如何查询缓存的日志？

A: 通过HTTP API接口查询：
- `GET /logs?chain_id=1` - 查询指定链的日志
- `GET /logs?from_block=19000000&to_block=19000010` - 查询指定区块范围的日志
- `GET /logs/stats` - 查看缓存统计信息
