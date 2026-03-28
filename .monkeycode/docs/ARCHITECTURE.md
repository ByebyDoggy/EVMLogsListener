# 系统架构设计

## 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     EVM Chain Listener                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │   Chain A    │    │   Chain B    │    │   Chain C    │     │
│  │  Listener    │    │  Listener    │    │  Listener    │     │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘     │
│         │                   │                   │               │
│         └───────────────────┼───────────────────┘               │
│                             │                                   │
│                    ┌────────▼────────┐                          │
│                    │  Event Router  │                          │
│                    └────────┬────────┘                          │
│                             │                                   │
│                    ┌────────▼────────┐                          │
│                    │   Log Cache     │                          │
│                    │  (内存缓存)      │                          │
│                    └─────────────────┘                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    External Services                             │
├─────────────────────────────────────────────────────────────────┤
│  ┌────────────┐  ┌────────────┐  ┌────────────┐               │
│  │ RPC Node 1 │  │ RPC Node 2 │  │ RPC Node N │               │
│  │ (Alchemy)  │  │ (Infura)   │  │ (QuickNode)│               │
│  └────────────┘  └────────────┘  └────────────┘               │
│                                                                 │
│  ┌────────────┐                                                │
│  │   Client   │  (查询缓存日志)                                │
│  │  Queries   │                                                │
│  └────────────┘                                                │
└─────────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. Chain Listener（链监听器）

每个Chain Listener负责监听一条EVM兼容链。

**职责**：
- 管理对应链的RPC节点池
- 维护区块高度进度
- 执行日志查询轮询
- 处理二分法补全逻辑

**设计**：
- 每个Chain使用独立的协程/线程
- 内部维护一个`RPCNodePool`进行节点管理

### 2. RPC Node Pool（RPC节点池）

管理多个RPC节点的访问，提供故障切换和负载均衡。

**职责**：
- 节点健康检查
- 请求速率限制跟踪
- 自动故障切换
- 轮询策略（Round-robin/随机）

**节点类型**：
- 公共节点（Rate limit严格）
- 付费节点（Alchemy/Infura/QuickNode）
- 自建节点

### 3. Block Range Query（二分法查询）

处理日志量超限时使用二分法拆分查询。

**流程**：
```
1. 查询 [fromBlock, toBlock] 区间日志
2. 若成功，返回结果
3. 若报错(日志量超限)：
   - 计算中点 mid = (fromBlock + toBlock) / 2
   - 递归查询 [fromBlock, mid] 和 [mid+1, toBlock]
4. 若某区间仍超限，继续二分直到区间为单个区块
```

### 4. Event Router（事件路由器）

接收所有链的日志事件，统一分发。

**职责**：
- 接收Chain Listener产生的日志事件
- 按配置过滤（可选）
- 格式化日志数据
- 发送到Webhook Dispatcher

### 5. Log Cache（日志缓存）

内存缓存，存储待查询的日志事件。

**职责**：
- 存储Chain Listener获取的日志
- FIFO淘汰策略
- 缓存大小限制（max_size）
- 过期日志自动清除
- 支持按链、时间、区块过滤查询
- 支持分页查询
- 去重处理

**配置参数**：
- `max_size`: 缓存最大容量（默认10000条）

### 6. Query API（查询接口）

提供HTTP接口供外部查询缓存的日志。

**接口**：
- `GET /logs` - 查询日志
- `GET /health` - 健康检查

## 数据流

```
1. Chain Listener 定时触发查询
         │
         ▼
2. RPCNodePool 获取可用节点
         │
         ▼
3. eth_getLogs 请求发送到RPC
         │
         ├──成功──▶ 4. Log Cache 入缓存
         │
         └──失败──▶ 检查错误类型
                        │
                        ├──限流──▶ 切换节点 + 指数退避重试
                        │
                        ├──日志超限──▶ 二分法拆分查询
                        │
                        └──网络错误──▶ 切换节点 + 重试

5. 外部客户端 通过 GET /logs 查询缓存日志
```

## 配置结构

```yaml
chains:
  - name: ethereum
    chain_id: 1
    poll_interval: 30  # 秒
    rpc_nodes:
      - url: "${ALCHEMY_ETH_URL}"
        priority: 1
        type: alchemy
      - url: "${INFURA_ETH_URL}"
        priority: 2
        type: infura
      - url: "https://eth.public-rpc.com"
        priority: 3
        type: public

  - name: bsc
    chain_id: 56
    poll_interval: 30
    rpc_nodes:
      - url: "${BSC_RPC_URL}"
        priority: 1
        type: paid

  - name: polygon
    chain_id: 137
    poll_interval: 30
    rpc_nodes:
      - url: "${POLYGON_RPC_URL}"
        priority: 1
        type: paid

cache:
  max_size: 10000      # 缓存最大日志条数

log:
  level: INFO
  format: json

api:
  host: "0.0.0.0"
  port: 8080
```

## 错误处理策略

| 错误类型 | 处理策略 |
|---------|---------|
| 429 Rate Limit | 切换到下一个节点，退避重试 |
| -32005 Log filter too large | 二分法拆分查询区间 |
| Connection Timeout | 切换节点，立即重试 |
| Invalid Response | 切换节点，记录错误 |
| Webhook Failed | 指数退避重试，最大3次 |

## 容错设计

1. **节点级别容错**
   - 每个链配置多个RPC节点
   - 单节点故障不影响整体服务
   - 自动踢除不健康节点

2. **请求级别容错**
   - 所有RPC请求都有重试机制
   - 超时时间可配置（默认30s）
   - 幂等性设计

3. **服务级别容错**
   - 单链故障不影响其他链
   - 健康检查机制
   - 优雅关闭（处理完当前请求）

## 性能考虑

- **并发查询**：多链独立并发轮询
- **异步IO**：使用asyncio处理网络请求
- **批量处理**：日志批量发送到Webhook
- **连接复用**：HTTP连接池

## 扩展点

- [ ] 支持更多EVM兼容链（Arbitrum、Optimism等）
- [ ] 支持消息队列（Kafka/RabbitMQ）作为输出
- [ ] 支持数据库存储日志
- [ ] 支持Prometheus指标导出
- [ ] 支持多Webhook负载均衡
