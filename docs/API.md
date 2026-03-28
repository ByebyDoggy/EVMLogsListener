# API 文档

## 基础信息

- 基础 URL: `http://localhost:8080`
- 响应格式: JSON
- 编码: UTF-8

## 端点列表

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/` | 服务信息 |
| GET | `/logs` | 查询日志列表 |
| GET | `/logs/stats` | 缓存统计信息 |
| GET | `/health` | 健康检查 |
| GET | `/ready` | 就绪检查 |

---

## GET /

获取服务基本信息和可用端点。

### 响应示例

```json
{
  "name": "EVM Chain Listener",
  "version": "1.0.0",
  "endpoints": [
    "GET /logs",
    "GET /logs/stats",
    "GET /health",
    "GET /ready"
  ]
}
```

---

## GET /logs

查询缓存的日志列表，支持多条件过滤和分页。

### 请求参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `chain_id` | integer | 否 | 链 ID 过滤 |
| `from_block` | integer | 否 | 最小区块号 |
| `to_block` | integer | 否 | 最大区块号 |
| `from_time` | string | 否 | 开始时间 (ISO8601) |
| `to_time` | string | 否 | 结束时间 (ISO8601) |
| `address` | string | 否 | 合约地址 |
| `page` | integer | 否 | 页码，默认 1 |
| `page_size` | integer | 否 | 每页条数，默认 100，最大 1000 |

### 请求示例

```bash
# 查询以太坊链最近日志
curl "http://localhost:8080/logs?chain_id=1&page=1&page_size=50"

# 查询指定区块范围
curl "http://localhost:8080/logs?from_block=18000000&to_block=18000100"

# 查询指定合约
curl "http://localhost:8080/logs?address=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

# 查询时间范围
curl "http://localhost:8080/logs?from_time=2024-01-01T00:00:00Z&to_time=2024-01-02T00:00:00Z"
```

### 响应示例

```json
{
  "status": "success",
  "data": {
    "logs": [
      {
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "topics": [
          "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
          "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045",
          "0x000000000000000000000000742d35cc6634c0532925a3b844bc9e7595f4b8a0"
        ],
        "data": "0x0000000000000000000000000000000000000000000000000000000000989680",
        "block_number": "0x112a880",
        "transaction_hash": "0x123abc...",
        "log_index": "0x42",
        "transaction_index": "0x15",
        "block_hash": "0xdef456...",
        "removed": false,
        "chain_id": 1,
        "chain_name": "ethereum",
        "timestamp": "2024-01-15T10:30:00+00:00"
      }
    ],
    "pagination": {
      "page": 1,
      "page_size": 100,
      "total": 1523,
      "total_pages": 16
    }
  }
}
```

### 错误响应

```json
{
  "status": "error",
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "Invalid page_size: must be positive integer"
  }
}
```

---

## GET /logs/stats

获取缓存统计信息。

### 请求示例

```bash
curl "http://localhost:8080/logs/stats"
```

### 响应示例

```json
{
  "status": "success",
  "data": {
    "total_logs": 8456,
    "max_size": 10000,
    "by_chain": {
      "1": 5000,
      "56": 3456
    },
    "oldest_log_timestamp": "2024-01-15T08:00:00+00:00",
    "newest_log_timestamp": "2024-01-15T12:30:00+00:00"
  }
}
```

---

## GET /health

健康检查端点，返回服务健康状态和各链监听器状态。

### 请求示例

```bash
curl "http://localhost:8080/health"
```

### 响应示例

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime": 3600,
  "chains": {
    "ethereum": {
      "status": "running",
      "last_block": 18001234,
      "last_poll": "2024-01-15T12:30:00+00:00"
    },
    "bsc": {
      "status": "running",
      "last_block": 35000000,
      "last_poll": "2024-01-15T12:29:55+00:00"
    }
  },
  "rpc_nodes": {
    "ethereum": {
      "active": "https://ethereum-rpc.publicnode.com",
      "available": [
        "https://ethereum-rpc.publicnode.com",
        "https://1rpc.io/eth"
      ],
      "failed": []
    },
    "bsc": {
      "active": "https://bsc-dataseed.binance.org",
      "available": ["https://bsc-dataseed.binance.org"],
      "failed": ["https://bsc-dataseed2.binance.org"]
    }
  }
}
```

### 状态值

| status | 描述 |
|--------|------|
| `healthy` | 所有链监听器正常运行 |
| `degraded` | 部分链或 RPC 节点异常 |

---

## GET /ready

就绪检查，用于 Kubernetes 探针。

### 请求示例

```bash
curl "http://localhost:8080/ready"
```

### 响应示例

```json
{
  "ready": true,
  "checks": {
    "config_loaded": true,
    "rpc_nodes_configured": true
  }
}
```

---

## 日志对象结构

| 字段 | 类型 | 描述 |
|------|------|------|
| `address` | string | 合约地址 |
| `topics` | string[] | 事件主题列表 |
| `data` | string | 事件数据（十六进制） |
| `block_number` | string | 区块号（十六进制） |
| `transaction_hash` | string | 交易哈希 |
| `log_index` | string | 日志索引（十六进制） |
| `transaction_index` | string | 交易索引（十六进制） |
| `block_hash` | string | 区块哈希 |
| `removed` | boolean | 是否被移除（重组） |
| `chain_id` | integer | 链 ID |
| `chain_name` | string | 链名称 |
| `timestamp` | string | 时间戳 (ISO8601) |

---

## 错误码

| 错误码 | HTTP 状态码 | 描述 |
|--------|-------------|------|
| `INVALID_PARAMETER` | 400 | 参数格式错误 |
| `INTERNAL_ERROR` | 500 | 内部服务错误 |

---

## 使用示例

### Python

```python
import requests

# 查询日志
response = requests.get(
    "http://localhost:8080/logs",
    params={
        "chain_id": 1,
        "page": 1,
        "page_size": 50
    }
)
data = response.json()
for log in data["data"]["logs"]:
    print(f"Block {int(log['block_number'], 16)}: {log['address']}")
```

### JavaScript

```javascript
// 查询日志
const response = await fetch(
  'http://localhost:8080/logs?chain_id=1&page=1&page_size=50'
);
const data = await response.json();
console.log(`Total logs: ${data.data.pagination.total}`);
```

### 监控集成

```yaml
# Prometheus 黑盒探针配置
modules:
  http_2xx:
    prober: http
    http:
      valid_http_codes: [200]
      method: GET
      fail_if_not_ssl: false
```

```yaml
# Kubernetes liveness/readiness 探针
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```
