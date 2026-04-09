# EVMLogListener → AlertProcessor 日志输入规格书

> **用途**: EVMLogListener 作为纯日志采集管道，定时向 AlertProcessor 推送增量区块事件日志。
> AlertProcessor 负责全部规则链匹配、可疑交易筛选和深度恶意行为分析。
>
> **数据方向**: EVMLogListener (生产者) → AlertProcessor (消费者)
>
> **职责边界**:
> - **EVMLogListener**: 只负责从链上抓取 Event Logs 并原样推送，**不做任何判断**
> - **AlertProcessor**: 接收原始日志 → 规则链过滤 → 可疑交易深度分析

---

## 一、架构总览

```
┌───────────────────────┐         ┌──────────────────────────────────┐
│    EVMChainListener    │         │        AlertProcessor             │
│                       │         │                                   │
│  eth_getLogs 轮询      │  HTTP   │  ┌─────────────────────────────┐ │
│  ↓ FIFO 缓存 (内存)     │  POST   │  │ ① 接收入口                  │ │
│  ↓ 定时刷新 (每 N 秒)   │ ──────→ │  │    POST /ingest/logs       │ │
│  ↓ 取出增量 logs       │  增量    │  ├─────────────────────────────┤ │
│  ↓ 原样打包推送         │  批量    │  │ ② 规则链引擎               │ │
│                       │         │  │    - 大额转账检测            │ │
│  [不判断、不分析]       │         │  │    - 闪电贷模式匹配          │ │
│                       │         │  │    - 多 Token 混合检测       │ │
│                       │         │  │    - 协议交互异常检测        │ │
│                       │         │  ├─────────────────────────────┤ │
│                       │         │  │ ③ 匹配成功 → 深度分析       │ │
│                       │         │  │    debug_traceTransaction   │ │
│                       │         │  │    → CallTree + 行为检测     │ │
│                       │         │  └─────────────────────────────┘ │
└───────────────────────┘         └──────────────────────────────────┘
```

### 核心原则

| 原则 | 说明 |
|------|------|
| **管道化** | EVMLogListener 只做搬运工，不添加任何业务逻辑 |
| **原样传输** | 推送的数据与 RPC 返回的 eth_getLogs 结果一致，不丢失字段 |
| **增量推送** | 仅推送自上次推送以来的新增日志，避免重复 |
| **容错** | 网络中断恢复后自动补传缺失区间的日志 |

---

## 二、API 规格：AlertProcessor 需提供的接收接口

### 核心: 接收增量日志

```
POST /ingest/logs
Content-Type: application/json
```

#### 请求体（由 EVMLogListener 发送）

```jsonc
{
  // ===== 必填 =====

  "chain_id": 1,
  "chain_name": "ethereum",

  // 本次推送的增量日志列表（按 block_number + log_index 排序）
  "logs": [
    {
      "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
      "topics": [
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045",
        "0x000000000000000000000000742d35cc6634c0532925a3b844bc9e7595f4b8a0"
      ],
      "data": "0x0000000000000000000000000000000000000000000000000000000000989680",
      "block_number": 19584123,
      "transaction_hash": "0xc310a0affe2169d1f6feec1c63dbc7f7c62a887fa48795d327d4d2da2d6b111d",
      "log_index": 42,
      "transaction_index": 15,
      "block_hash": "0xd49b57e5a7b8cc8d96eb13f39cbbca2fbf6b4e3a7351d92e0c611ffea4b8b44d",
      "removed": false
    },
    {
      "address": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
      "topics": ["0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed800448115cc1f620", "..."],
      "data": "0x...",
      "block_number": 19584123,
      "transaction_hash": "0xc310a0affe2169d1f6feec1c63dbc7f7c62a887fa48795d327d4d2da2d6b111d",
      "log_index": 43,
      "transaction_index": 15,
      "block_hash": "0xd49b57...",
      "removed": false
    }
    // ... 更多 logs
  ],

  // ===== 区间信息 =====

  "from_block": 19584120,     // 本批次起始区块（含）
  "to_block": 19584123,       // 本批次结束区块（含）
  "log_count": 156,           // 本批次日志总数
  "pushed_at": "2026-04-07T12:30:45.123Z"
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `chain_id` | integer | ✅ | 链 ID (1=ETH, 56=BSC, 137=Polygon...) |
| `chain_name` | string | ✅ | 链名称标识 |
| `logs` | array[LogObject] | ✅ | 增量事件日志数组 |
| `from_block` | integer | ✅ | 本批次覆盖的起始区块号 |
| `to_block` | integer | ✅ | 本批次覆盖的结束区块号 |
| `log_count` | integer | ✅ | `logs` 数组长度（方便快速校验） |
| `pushed_at` | ISO8601 datetime | ✅ | 推送时间戳 |

#### LogObject 内部字段（每个 log 条目）

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `address` | string | RPC | 触发事件的合约地址 |
| `topics` | string[] | RPC | 事件主题数组，`topics[0]`=事件签名哈希 |
| `data` | string | RPC | 未编码的事件 ABI 数据（hex字符串，带 0x 前缀） |
| `block_number` | integer | RPC | 所在区块号 |
| `transaction_hash` | string | RPC | 所属交易的 hash |
| `log_index` | integer | RPC | 在该交易内的日志序号 |
| `transaction_index` | integer | RPC | 该交易在区块中的索引 |
| `block_hash` | string | RPC | 区块哈希 |
| `removed` | boolean | RPC | 是否因链重组被移除 |

#### 成功响应

```json
{
  "status": "accepted",
  "received_log_count": 156,
  "from_block": 19584120,
  "to_block": 19584123,
  "matched_alert_count": 3,          // 规则链命中的可疑交易数
  "processed_at": "2026-04-07T12:30:46.500Z",
  "next_push_after_seconds": 5        // 建议 EVMLogListener 的下次推送间隔
}
```

#### 错误响应

```json
// 参数校验失败
{ "status": "error", "code": 400, "detail": "Missing required field: chain_id" }

// 服务繁忙（限流）
{ "status": "error", "code": 429, "detail": "Too many requests", "retry_after": 10 }

// 内部错误
{ "status": "error", "code": 500, "detail": "Internal server error" }
```

---

### 辅助: 同步状态确认

```
GET /ingest/status
```

用于 EVMLogListener 启动或断线恢复后确认 AlertProcessor 当前消费进度：

```json
{
  "is_connected": true,
  "last_received_at": "2026-04-07T12:30:45.123Z",
  "consumed_blocks": {
    "1": {                    // chain_id
      "last_block": 19584123,
      "total_logs_received": 1250460,
      "total_matched_alerts": 89
    },
    "56": {
      "last_block": 38245101,
      "total_logs_received": 892340,
      "total_matched_alerts": 34
    }
  }
}
```

---

### 辅助: 补传历史日志

```
POST /ingest/logs/replay
Content-Type: application/json
```

用于 EVMLogListener 断线重连后补传缺失区间：

```json
{
  "chain_id": 1,
  "from_block": 19584000,
  "to_block": 19584119,
  "reason": "reconnection_gap",
  "logs": [ /* 与主接口相同的 LogObject 格式 */ ]
}
```

---

## 三、EVMLogListener 推送机制规格

### 3.1 推送触发策略

EVMLogListener 应支持两种模式：

| 模式 | 触发条件 | 适用场景 |
|------|----------|----------|
| **定时推送** | 固定间隔 N 秒（默认 5s） | 正常运行，低延迟 |
| **阈值推送** | 缓存中累积 ≥ M 条日志（默认 100条） | 高吞吐量场景 |

两者满足任一即触发推送（取较早触发的那个）。

### 3.2 推送流程伪代码

```python
class LogPusher:
    """EVMLogListener 侧的增量日志推送器"""

    def __init__(self, target_url: str, push_interval_sec: float = 5.0, batch_size: int = 200):
        self._target = target_url.rstrip("/")
        self._interval = push_interval_sec
        self._batch_size = batch_size

        # 状态追踪 —— 用于断线续传
        self._last_pushed_block: dict[int, int] = {}  # chain_id -> 已推送到的最大区块
        self._pending_buffer: list[Log] = []           # 待推送缓冲区
        self._http_session: aiohttp.ClientSession | None = None

    async def on_new_logs(self, logs: list[Log], chain_id: int):
        """
        由 ChainListener._on_logs_received() 回调调用
        将新抓取的 logs 加入待推送队列
        """
        self._pending_buffer.extend(logs)
        
        # 达到阈值立即推送
        if len(self._pending_buffer) >= self._batch_size:
            await self._flush(chain_id)

    async def _periodic_flush(self):
        """后台定时任务：每隔 interval 秒检查并推送"""
        while True:
            await asyncio.sleep(self._interval)
            if self._pending_buffer:
                # 按链分组推送
                by_chain = self._group_by_chain(self._pending_buffer)
                for cid, chain_logs in by_chain.items():
                    await self._send_batch(cid, chain_logs)

    async def _send_batch(self, chain_id: int, logs: list[Log]):
        """构建请求体并发送到 AlertProcessor"""
        if not logs:
            return

        block_nums = [l.block_number for l in logs]
        payload = {
            "chain_id": chain_id,
            "chain_name": self._chain_name_of(chain_id),
            "logs": [self._log_to_dict(l) for l in logs],
            "from_block": min(block_nums),
            "to_block": max(block_nums),
            "log_count": len(logs),
            "pushed_at": datetime.utcnow().isoformat() + "Z",
        }

        try:
            async with self._http_session.post(
                f"{self._target}/ingest/logs",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 202):
                    result = await resp.json()
                    last_block = max(block_nums)
                    self._last_pushed_block[chain_id] = last_block
                    logger.info(
                        f"[pusher] Pushed {len(logs)} logs for chain {chain_id} "
                        f"(blocks {min(block_nums)}-{max(block_nums)}) "
                        f"→ alerts={result.get('matched_alert_count', '?')}"
                    )
                    return True
                elif resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"[pusher] Rate limited, retry after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    return await self._send_batch(chain_id, logs)  # 重试一次
                else:
                    logger.error(f"[pusher] Failed with status {resp.status}")
                    return False

        except Exception as e:
            logger.error(f"[pusher] Error: {e}")
            return False

    def _log_to_dict(self, log: Log) -> dict:
        """Log → dict 序列化（兼容 AlertProcessor 期望格式）"""
        return {
            "address": log.address,
            "topics": log.topics,
            "data": log.data,
            "block_number": log.block_number,
            "transaction_hash": log.transaction_hash,
            "log_index": log.log_index,
            "transaction_index": log.transaction_index,
            "block_hash": log.block_hash,
            "removed": log.removed,
        }
```

### 3.3 断线恢复策略

```
正常状态:
  last_pushed_block[1] = 19584120  ← 上次成功推送到的区块
  
网络中断持续 3 个轮询周期:

恢复后:
  ① 调用 GET /ingest/status → 获取 AlertProcessor 侧已消费的最大区块
  ② 对比本地缓存中最小区块 vs 远程已消费区块
  ③ 如有缺口 (gap):
     - 从本地 LogCache 中取出 gap 区间的 logs
     - 通过 POST /ingest/logs/replay 发送补传请求
     - 标记 gap 已填补
  ④ 之后恢复正常增量推送
```

### 3.4 配置项

```yaml
# config.yaml 新增 section
alert_processor:
  enabled: true
  url: "http://localhost:8000"

  # 推送策略
  push_interval_seconds: 5        # 定时推送间隔（秒）
  batch_size: 200                 # 阈值：攒够多少条就立即推送
  max_payload_mb: 10              # 单次推送最大 payload 大小（超过则拆分）

  # 重试
  retry_attempts: 3
  retry_base_delay_sec: 1          # 指数退避基数 (1s → 2s → 4s)
  timeout_seconds: 10             # HTTP 请求超时

  # 断线恢复
  reconnect_check_on_startup: true  # 启动时检查是否需要补传
  replay_endpoint: "/ingest/logs/replay"
```

---

## 四、AlertProcessor 侧处理流水线

### 4.1 接收到 logs 后的处理步骤

```
POST /ingest/logs 收到 156 条 logs
        │
        ▼
  ┌─────────────────────────────────────┐
  │ Step 1: 快速写入缓冲区              │
  │   - 写入内存环形缓冲区 / SQLite     │
  │   - 记录 from_block / to_block 进度 │
  │   - 立即返回 202 Accepted           │
  └──────────────────┬──────────────────┘
                     │ 异步处理
                     ▼
  ┌─────────────────────────────────────┐
  │ Step 2: 按 transaction_hash 分组    │
  │                                     │
  │   tx_0xc310... : [log_idx=42,43,44] │
  │   tx_0xd456... : [log_idx=50]       │
  │   tx_0x789a... : [log_idx=51,52]    │
  │   ... 共涉及 47 笔不同交易           │
  └──────────────────┬──────────────────┘
                     │
                     ▼
  ┌─────────────────────────────────────┐
  │ Step 3: 规则链引擎 (Rule Chain)     │
  │                                     │
  │   Rule 1: 大额 Transfer?            │
  │     topics[0]==Transfer && value>$K │
  │                                     │
  │   Rule 2: 闪电贷特征?               │
  │     Aave Pool addr + 大额进出       │
  │                                     │
  │   Rule 3: 多 Token 交互?            │
  │     单笔 tx >N 个不同合约地址        │
  │                                     │
  │   Rule 4: DEX 聚合器调用?           │
  │     Uniswap/Balancer/1inch Router   │
  │                                     │
  │   ... 可插拔扩展更多规则             │
  │                                     │
  │   结果: 3/47 笔交易命中规则 → 入队   │
  └──────────────────┬──────────────────┘
                     │ 命中
                     ▼
  ┌─────────────────────────────────────┐
  │ Step 4: 深度分析 (异步任务队列)     │
  │                                     │
  │   对每笔命中规则的 tx:              │
  │   ├─ debug_traceTransaction(tx)    │
  │   ├─ eth_getTransactionReceipt(tx) │
  │   ├─ eth_getTransactionByHash(tx)  │
  │   ├─ 构建完整 CallTree             │
  │   ├─ 函数签名解析                   │
  │   ├─ 协议标签识别                   │
  │   ├─ 行为检测(闪电贷/套利/三明治)   │
  │   └─ Balance Changes 计算          │
  └──────────────────┬──────────────────┘
                     │
                     ▼
  ┌─────────────────────────────────────┐
  │ Step 5: 结果输出                     │
  │                                     │
  │   a) 写入 alerts.db (SQLite)        │
  │   b) WebSocket 实时推送给前端       │
  │   c) 回调通知 EVMLogListener(可选)  │
  │   d) 触发后续动作(邮件/webhook)     │
  └─────────────────────────────────────┘
```

### 4.2 规则链引擎示例

```python
# detectors/trace/rule_engine.py （AlertProcessor 侧新增）

from dataclasses import dataclass
from typing import Protocol

@dataclass
class RuleResult:
    """规则匹配结果"""
    matched: bool
    alert_type: str = ""           # "large_transfer" | "flash_loan" | ...
    severity: str = "low"          # "info" | "low" | "medium" | "high" | "critical"
    confidence: float = 0.0        # 0.0 ~ 1.0
    details: dict | None = None    # 附带详情


class DetectionRule(Protocol):
    """检测规则接口"""
    
    name: str
    priority: int                   # 数值越小优先执行
    
    def evaluate(self, tx_logs: list[dict]) -> RuleResult:
        """评估一笔交易的所有 logs 是否命中本规则"""
        ...


class LargeTransferRule:
    """规则: 大额 ERC20 转账"""
    
    name = "large_transfer"
    priority = 10
    
    # 配置: 各代币的最小金额阈值（raw units）
    THRESHOLDS: dict[str, tuple[int, int]] = {
        # (合约地址, decimals) → 最小 raw value
        ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6): 100_000 * 10**6,   # USDC > $100K
        ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6): 100_000 * 10**6,   # USDT > $100K
        ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18): 10 * 10**18,      # WETH > 10 ETH
    }
    
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    
    def evaluate(self, tx_logs: list[dict]) -> RuleResult:
        total_value_usd = 0.0
        
        for log in tx_logs:
            if not log.get("topics") or log["topics"][0] != self.TRANSFER_TOPIC:
                continue
            
            token_addr = log["address"].lower()
            
            # 解码 transfer value (topic[2]=value, 但实际在 data 字段中)
            try:
                value_raw = int(log["data"], 16)
            except (ValueError, IndexError):
                continue
            
            # 查找阈值配置
            threshold = None
            for (addr, dec), thresh in self.THRESHOLDS.items():
                if token_addr == addr.lower():
                    # TODO: 从 MarketDataBase 获取实时价格做 USD 换算
                    # 这里先用简单比较
                    if value_raw >= thresh:
                        total_value_usd += value_raw / (10 ** dec)
                        break
        
        if total_value_usd >= 10000:  # 总额 > $10K
            return RuleResult(
                matched=True,
                alert_type="large_transfer",
                severity="medium" if total_value_usd < 100000 else "high",
                confidence=min(total_value_usd / 100000, 1.0),
                details={"estimated_value_usd": round(total_value_usd, 2)}
            )
        
        return RuleResult(matched=False)


class FlashLoanPatternRule:
    """规则: 闪电贷模式检测（基于已知协议地址 + 特征 pattern）"""
    
    name = "flash_loan_pattern"
    priority = 5  # 高优先级，先于通用规则执行
    
    # 已知闪电贷提供方
    FLASH_LOAN_PROVIDERS = {
        "0x7d2768dE32b0b80b7a3454c06B0Ac3200957b515",  # Aave V2 Pool
        "0x0De8F8eAdD1B1204364c4e99Ed2E2b114Af9E76d",  # dYdx Solo Margin
        "0xE0f5204B360ee48fE1E310A4E1fFC716C8f538ba",  # Balancer Vault
    }
    
    # 闪电贷相关事件 topic
    FLASH_LOAN_TOPICS = {
        "0x6b1277772e905ce5e7a4bb572f0e33e98e7e589ee0acf0bd5a85c0ef6a5c26e1",  # Aave FlashLoan
        "0x0c6c8e6eab042c4efced994654bcae53c2060cb1f03e84efa2d9ee0bf0b5ae98",  # Balancer FlashLoan
    }
    
    def evaluate(self, tx_logs: list[dict]) -> RuleResult:
        has_flash_loan_event = False
        involved_protocols = set()
        
        for log in tx_logs:
            addr = log.get("address", "").lower()
            
            # 检查是否来自闪电贷协议
            if addr in self.FLASH_LOAN_PROVIDERS:
                involved_protocols.add(addr)
            
            # 检查闪电贷事件签名
            topics = log.get("topics", [])
            if topics and topics[0] in self.FLASH_LOAN_TOPICS:
                has_flash_loan_event = True
        
        if has_flash_loan_event or involved_protocols:
            return RuleResult(
                matched=True,
                alert_type="flash_loan",
                severity="high",
                confidence=0.9 if has_flash_loan_event else 0.6,
                details={
                    "protocols": list(involved_protocols),
                    "has_flash_event": has_flash_loan_event,
                    "log_count": len(tx_logs),
                }
            )
        
        return RuleResult(matched=False)


class MultiTokenInteractionRule:
    """规则: 单笔交易中涉及过多不同代币合约"""
    
    name = "multi_token_interaction"
    priority = 20
    
    MAX_TOKEN_CONTRACTS = 8       # 超过此数量标记为可疑
    MIN_LOGS_PER_TX = 5           # 至少这么多条 log 才评估
    
    def evaluate(self, tx_logs: list[dict]) -> RuleResult:
        if len(tx_logs) < self.MIN_LOGS_PER_TX:
            return RuleResult(matched=False)
        
        unique_contracts = set()
        for log in tx_logs:
            addr = log.get("address", "").lower()
            if addr:
                unique_contracts.add(addr)
        
        count = len(unique_contracts)
        if count > self.MAX_TOKEN_CONTRACTS:
            return RuleResult(
                matched=True,
                alert_type="token_mixing",
                severity="medium",
                confidence=min((count - self.MAX_TOKEN_CONTRACTS) / 5, 1.0),
                details={"unique_token_contracts": count, "contracts": list(unique_contracts)[:10]}
            )
        
        return RuleResult(matched=False)


# 规则链编排
RULE_CHAIN = [
    FlashLoanPatternRule(),       # 优先: 闪电贷（高价值目标）
    LargeTransferRule(),          # 其次: 大额转账
    MultiTokenInteractionRule(),  # 最后: 多 Token 混合
    # ... 未来扩展:
    # SandwichAttackRule(),
    # ArbitragePatternRule(),
    # ApprovalDrainRule(),
]
```

---

## 五、数据流完整示例

### 场景: 一个区块内发生多笔交易

**EVMLogListener 抓到区块 #19584123 的 156 条 Event Logs**

#### T+0s: EVMLogListener 轮询获取新区块

```
eth_getLogs(fromBlock=19584123, toBlock=19584123)
→ 返回 156 条 logs（属于 47 笔不同交易）
→ 存入 FIFO 缓存 (LogCache.add_many)
```

#### T+3s: 定时推送触发（push_interval=5s，但 batch_size 先达）

```
缓冲区累计 200 条 → 达到 batch_size 阈值
→ 构建 payload:
  {
    chain_id: 1,
    from_block: 19584120,
    to_block: 19584123,
    logs: [...200条...],
    log_count: 200
  }

→ POST http://localhost:8000/ingest/logs
→ 收到 202 Accepted:
  {
    status: "accepted",
    received_log_count: 200,
    matched_alert_count: 5,
    next_push_after_seconds: 5
  }
```

#### T+3.1s: AlertProcessor 异步处理

```
Step 1 - 写入缓冲区: 200 条 logs 入库
Step 2 - 按 tx_hash 分组: 200条 → 61笔不同交易
Step 3 - 规则链逐笔评估:
  
  tx_0xc310... (12条logs):
    ├─ FlashLoanPatternRule  → ✅ HIT! (Aave V2 + Balancer Vault)
    ├─ LargeTransferRule     → ✅ HIT! (500 ETH)
    └─ MultiTokenInteraction → ✅ HIT! (15个不同合约)
    → 最高优先级: flash_loan (severity=high)
    → 进入深度分析队列 ★
  
  tx_0xd456... (3条logs):
    ├─ FlashLoanPatternRule  → ❌ miss
    ├─ LargeTransferRule     → ❌ miss ($50 < $10K)
    └─ MultiTokenInteraction → ❌ miss (3个合约 < 8)
    → 无匹配 → 跳过
  
  tx_0x789a... (25条logs):
    ├─ FlashLoanPatternRule  → ❌ miss
    ├─ LargeTransferRule     → ✅ HIT! (USDC $250K)
    └─ MultiTokenInteraction → ✅ HIT! (12个合约)
    → 进入深度分析队列 ★
  
  ... 其余 58 笔全部跳过

Step 4 - 深度分析 (异步并行):
  
  分析 tx_0xc310...:
    debug_traceTransaction → 150 帧 CallTree
    eth_getReceipt         → 12 条 logs
    eth_getTxByHash        → 元信息
    → 最终结果: 闪电贷套利攻击, profit=$12,450
    
  分析 tx_0x789a...:
    debug_traceTransaction → 280 帧 CallTree (复杂的 MEV bot)
    → 最终结果: 三明治攻击受害者

Step 5 - 结果输出:
  → 写入 alerts.db
  → WebSocket 推送到前端 Dashboard
  → 触发 webhook 通知
```

---

## 六、双方改造清单

### EVMLogListener 侧需做的

| # | 任务 | 文件 | 工作量 |
|---|------|------|--------|
| 1 | 新增 `LogPusher` 类 | `src/evm_chain_listener/pusher.py` (新文件) | ~150行 |
| 2 | 在 `main.py` lifespan 中初始化 Pusher | `src/evm_chain_listener/main.py` | ~20行修改 |
| 3 | `ChainListener._on_logs_received()` 回调中接入 Pusher | `src/evm_chain_listener/chains/base.py` | ~10行修改 |
| 4 | config.yaml 新增 `alert_processor` section | `config.yaml` | ~20行新增 |
| 5 | 断线恢复逻辑 (启动时 check + replay) | `pusher.py` | ~60行 |
| 6 | 推送统计指标 (成功率/延迟/积压量) | 复用现有 `/api/logs/stats` 或新增 | ~30行 |

**总计**: ~290行新代码，主要是一个 HTTP 推送客户端 + 缓冲管理

### AlertProcessor 侧需做的

| # | 任务 | 文件 | 工作量 |
|---|------|------|--------|
| 1 | 新增 `POST /ingest/logs` 接收端点 | `routers/detectors/ingest_router.py` (新文件) | ~120行 |
| 2 | 新增 `GET /ingest/status` 状态查询 | 同上 | ~40行 |
| 3 | 新增 `POST /ingest/logs/replay` 补传端点 | 同上 | ~40行 |
| 4 | 规则链引擎框架 (`DetectionRule` 协议 + `RuleEngine`) | `detectors/trace/rule_engine.py` (新文件) | ~200行 |
| 5 | 实现 3-5 条内置检测规则 | `detectors/trace/rules/` (新目录) | ~300行 |
| 6 | 异步分析任务队列 (匹配 → 深度分析) | `engine/task_queue.py` (新文件) | ~150行 |
| 7 | 消费进度持久化 (per-chain last_block) | `database/ingestion_progress.py` (新文件) | ~80行 |
| 8 | 速率限制中间件 | `middleware/rate_limiter.py` (新文件) | ~50行 |

**总计**: ~980行新代码，主要是规则链引擎和分析队列

---

## 七、性能考量

### 7.1 吞吐量估算

| 链 | 平均每区块 logs | 区块时间 | 推送频率 | 估算 QPS |
|----|-----------------|---------|---------|---------|
| Ethereum | ~200-500 | 12s | 每 5s | ~40 logs/s |
| BSC | ~300-800 | 3s | 每 3s | ~150 logs/s |
| Polygon | ~100-300 | 2s | 每 2s | ~100 logs/s |

### 7.2 AlertProcessor 处理能力要求

- **日志接收**: 纯写操作，应能轻松支撑 500+ logs/s（SQLite WAL 模式下）
- **规则链匹配**: 内存计算，每笔交易微秒级，非瓶颈
- **深度分析**: 瓶颈所在！每笔需要 3-5 次 RPC 调用 (~2-5s/笔)
  - 需要异步队列 + 并发控制（建议同时分析 ≤ 10 笔）
  - 不匹配的交易直接丢弃，不进入分析队列

### 7.3 反压机制

当 AlertProcessor 处理不过来时：

```
AlertProcessor 返回:
  429 Too Many Requests
  Retry-After: 30

EVMLogListener 收到后:
  1. 停止推送
  2. 继续积累到本地缓存 (LogCache 有上限 10000)
  3. 30s 后重试
  4. 如果缓存快满了 → 丢掉最旧的 logs 并记录警告日志
```

---

## 八、错误处理规范

### EVMLogListener 侧

| 场景 | 处理方式 |
|------|----------|
| AlertProcessor 不可达 (连接拒绝) | 指数退避重试，同时继续收集 logs 到本地缓存 |
| AlertProcessor 返回 5xx | 重试 3 次，仍失败则记录日志，下次定时推送时包含这些数据 |
| AlertProcessor 返回 429 | 读取 `Retry-After` 头等待后重试 |
| AlertProcessor 返回 400 | 记录详细错误日志，丢弃这批数据（格式问题需人工排查） |
| 本地缓存即将溢出 | 丢弃最旧的数据，记录 `cache_overflow` 告警日志 |
| 网络超时 (>10s) | 放弃本次推送，数据保留在 buffer 中等下一轮 |

### AlertProcessor 侧

| 场景 | HTTP Status | 处理方式 |
|------|------------|----------|
| 请求体 JSON 格式非法 | 400 | 返回具体校验错误 |
| logs 为空数组 | 400 | `{"detail": "logs array must not be empty"}` |
| block 区间重叠/乱序 | 202 接受 + 内部去重 | 允许重复推送，基于 `(tx_hash, log_index)` 去重 |
| removed=true 的日志 | 202 接受 | 记录为链重组事件，可能需要撤销之前产生的告警 |
| 内部队列满 | 429 | 返回 `Retry-After`，让上游减速 |
| 数据库写入失败 | 500 | 返回错误，上游应重试 |

---

## 九、测试用例

### EVMLogListener → AlertProcessor 集成测试

#### Case 1: 正常增量推送

```json
// Request: 推送单个区块的 3 条 logs
{
  "chain_id": 1, "from_block": 100, "to_block": 100,
  "logs": [
    {"address":"0xA0b...", "topics":["0xddf25...","0x...","0x..."],
     "data":"0x...", "block_number":100, "tx_hash":"0xaa...",
     "log_index":0, "tx_index":0, "block_hash":"0x...", "removed":false},
    {"address":"0x7a25...", "topics":["0xc4207...","0x...","0x..."],
     "data":"0x...", "block_number":100, "tx_hash":"0xbb...",
     "log_index":0, "tx_index":1, "block_hash":"0x...", "removed":false},
    {"address":"0xC02a...", "topics":["0xddf25...","0x...","0x..."],
     "data":"0x...", "block_number":100, "tx_hash":"0xcc...",
     "log_index":0, "tx_index":2, "block_hash":"0x...", "removed":false}
  ]
}

// Expect: 202 Accepted
// Expect: matched_alert_count 可能是 0（取决于规则和金额大小）
// Expect: GET /ingest/status 显示 chain_id=1 的 last_block 更新为 100
```

#### Case 2: 含闪电贷特征的交易

```
推送包含以下 logs 的批次:
  - address=Aave Pool V2, topic=FlashLoan 事件
  - address=Uniswap V3 Router, topic=Swap 事件  
  - address=WETH, topic=Transfer (大额)

Expect: matched_alert_count >= 1
Expect: 命中规则 = flash_loan_pattern
Expect: 该 tx_hash 进入深度分析队列
```

#### Case 3: 重复推送（幂等性）

```
第一次推送 block 100 的 logs → 202 accepted
再次推送完全相同的 block 100 的 logs → 202 accepted (去重, 不重复分析)
```

#### Case 4: 断线补传 (replay)

```
正常推送到了 block 500
断网 10 分钟 (block 501~600)
重连后:
  POST /ingest/logs/replay { "chain_id":1, "from_block":501, "to_block":600, "logs":[...] }
→ 202 Accepted
→ AlertProcessor 对补传区间内的可疑交易进行回溯分析
```

#### Case 5: 高频压力测试

```
以 50 TPS 的速度连续推送 5 分钟 (15000 条 logs)
验证:
  - 无数据丢失
  - 内存稳定
  - 429 rate limit 正确触发和恢复
  - matched_alert_count 合理
```

---

## 十、实施路线图

### Phase 1: MVP（最小可跑通）

> 目标: EVMLogListener 能把 logs 推过来，AlertProcessor 能接住并通过规则链产生告警

**EVMLogListener (1天)**:
1. 实现 `LogPusher` 类（HTTP POST 推送 + 缓冲）
2. config.yaml 加 `alert_processor.url` 配置
3. `ChainListener` 回调中注入 pusher
4. 手动测试推送成功

**AlertProcessor (2-3天)**:
1. 实现 `POST /ingest/logs` 端点
2. 实现基础规则链框架 + 2 条规则 (LargeTransfer + FlashLoan)
3. 命中后打印日志（暂不做深度分析）
4. 实现 `GET /ingest/status`
5. 端到端联调

### Phase 2: 生产可用

> 目标: 稳定可靠、有监控、有反压

**EVMLogListener**:
6. 断线恢复 (replay) 机制
7. 推送统计 Dashboard 指标
8. 本地持久化 fallback（内存满时的磁盘溢写）

**AlertProcessor**:
9. 深度分析异步队列（命中规则 → 自动调 analyze()）
10. 消费进度持久化 (SQLite)
11. 速率限制 + 反压
12. 告警存储 (alerts.db)
13. WebSocket 实时推送给前端

### Phase 3: 智能增强

> 目标: 降低误报率，提升检测精度

14. 更多规则（三明治攻击套利、Approval 盗取、Tornado Cash 等）
15. 规则权重打分系统（多条规则命中时综合评分）
16. 历史回溯扫描（指定范围批量回放分析）
17. 误报反馈机制（前端标记 false positive → 自动降低对应规则置信度）

---

## 附录: Log 数据格式对照表

### EVMLogListener 现有 Log.to_dict() → AlertProcessor 期望格式

两者基本一致，仅需注意数值类型的转换：

| 字段 | EVMLogListener to_dict() | AlertProcessor ingest 期望 | 需要转换？ |
|------|-------------------------|---------------------------|-----------|
| `address` | `"0xA0b..."` (string) | string | 否 |
| `topics` | `["0xddf...", ...]` | string[] | 否 |
| `data` | `"0x..."` (hex string) | hex string | 否 |
| `blockNumber` | **`"0x112a880"`** (hex string) | **`19584123`** (integer) | **⚠️ 是: hex→int** |
| `transactionHash` | `"0x..."` (string) | string | 否 |
| `logIndex` | **`"0x42"`** (hex string) | **`42`** (integer) | **⚠️ 是: hex→int** |
| `transactionIndex` | **`"0x15"`** (hex string) | **`15`** (integer) | **⚠️ 是: hex→int** |
| `blockHash` | `"0x..."` (string) | string | 否 |
| `removed` | `true/false` (bool) | bool | 否 |
| `chainId` | `1` (int) | int | 否 |

> **重要**: EVMLogListener 的 `Log.to_dict()` 将 `blockNumber`/`logIndex`/`transactionIndex` 输出为 **hex 字符串**（因为这是 RPC 原始格式）。AlertProcessor 需要 **整数** 格式。`LogPusher._log_to_dict()` 中需要做这个转换。
