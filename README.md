# EVM Chain Listener

一个轻量级的 EVM 兼容区块链事件日志监听器，支持多链、多 RPC 节点、自动故障转移和二分查询优化。

## 功能特性

- **多链支持**: 同时监听多条 EVM 兼容链（Ethereum、BSC、Polygon 等）
- **多 RPC 节点**: 支持配置多个 RPC 节点，自动故障转移
- **智能查询**: 二分查询算法处理 `eth_getLogs` 结果过大错误
- **速率限制**: 内置请求限流，保护公共 RPC 节点
- **内存缓存**: FIFO 缓存存储日志，支持分页查询
- **Web 界面**: 内置日志查看和筛选前端
- **REST API**: FastAPI 驱动的高性能 HTTP 接口
- **跨平台**: 支持 Windows、Linux、macOS

## 快速开始

### 环境要求

- Python 3.10+
- pip 包管理器

### 安装

```bash
# 克隆项目
git clone <repository-url>
cd EVMLogListener

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 配置

复制配置模板并修改：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml` 配置文件，设置要监听的链和 RPC 节点。

### 运行

```bash
# 进入 src 目录运行
cd src

# Linux/macOS
python -m evm_chain_listener.main -c ../config.yaml

# Windows (使用虚拟环境)
..\..\\.venv\Scripts\python.exe -m evm_chain_listener.main -c ..\config.yaml
```

启动后访问 http://localhost:8080 查看 Web 界面。

## Web 界面

访问 `http://localhost:8080/` 打开日志查看界面，支持：

- **实时统计**: 总日志数、缓存大小、活跃链数
- **多条件筛选**: 链、事件类型、合约地址、区块范围、时间范围
- **日志详情**: 点击展开查看完整日志信息
- **自动刷新**: 可开关的自动更新功能

## 配置说明

### 完整配置示例

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

health:
  enabled: true
  endpoint: /health
```

### 配置项说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `chains[].name` | 链名称 | 必填 |
| `chains[].chain_id` | 链 ID | 必填 |
| `chains[].poll_interval` | 轮询间隔（秒） | 30 |
| `chains[].rpc_nodes[].url` | RPC 节点 URL | 必填 |
| `chains[].rpc_nodes[].priority` | 节点优先级（越小越优先） | 100 |
| `chains[].rpc_nodes[].type` | 节点类型（public/paid） | public |
| `cache.max_size` | 缓存最大日志数 | 10000 |
| `api.host` | API 监听地址 | 0.0.0.0 |
| `api.port` | API 监听端口 | 8080 |
| `log.level` | 日志级别 | INFO |
| `log.format` | 日志格式（json/text） | json |

### 环境变量

配置文件支持环境变量替换，使用 `${VAR_NAME}` 语法：

```yaml
rpc_nodes:
  - url: "${ALCHEMY_ETH_URL}"
    priority: 1
```

## API 接口

基于 FastAPI 构建，访问 `http://localhost:8080/docs` 查看交互式 API 文档。

### 获取日志列表

```
GET /api/logs?chain_id=1&from_block=1000000&page=1&page_size=100
```

查询参数：
- `chain_id`: 链 ID
- `from_block` / `to_block`: 区块范围
- `from_time` / `to_time`: 时间范围（ISO8601）
- `address`: 合约地址
- `topic`: 事件主题（第一个 topic）
- `page` / `page_size`: 分页参数

### 获取缓存统计

```
GET /api/logs/stats
```

### 获取唯一事件类型

```
GET /api/logs/topics
```

### 获取链状态

```
GET /api/chains
```

### 健康检查

```
GET /health
```

### 就绪检查

```
GET /ready
```

## Docker 部署

```bash
# 构建镜像
docker build -t evm-listener .

# 运行容器
docker run -d -p 8080:8080 -v $(pwd)/config.yaml:/app/config.yaml evm-listener
```

或使用 Docker Compose：

```bash
docker-compose up -d
```

## 项目结构

```
EVMLogListener/
├── src/
│   └── evm_chain_listener/
│       ├── main.py          # 入口文件 + FastAPI 应用
│       ├── config.py        # 配置加载
│       ├── models.py        # 数据模型
│       ├── templates/       # 前端模板
│       │   └── index.html
│       ├── api/             # API 模块（预留）
│       ├── cache/           # 日志缓存
│       │   └── log_cache.py
│       ├── chains/          # 链监听器
│       │   └── base.py
│       ├── rpc/             # RPC 交互
│       │   ├── node_pool.py # 节点池管理
│       │   ├── binary_search.py # 二分查询
│       │   └── exceptions.py    # 异常定义
│       └── utils/           # 工具函数
│           └── logging.py
├── docs/                    # 文档
├── tests/                   # 测试文件
├── config.yaml              # 配置文件
├── requirements.txt         # Python 依赖
├── Dockerfile
└── docker-compose.yaml
```

## 开发

### 运行测试

```bash
pip install pytest pytest-asyncio
pytest tests/
```

### 代码风格

项目使用 Python 类型注解，遵循 PEP 8 规范。

## 许可证

MIT License
