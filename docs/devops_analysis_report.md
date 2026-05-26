# EVMLogListener DevOps 运维改进分析报告

## 1. 可观测性增强

### 1.1 当前日志系统评估

**现有优势:**
- `utils/logging.py` 已实现 JSONFormatter 和 TextFormatter 双格式输出
- 支持 `extra` 字段注入结构化上下文（chain, from_block, to_block, log_count, duration_ms）
- 配置驱动：支持 level/format/output/file_path 四维配置
- 使用标准 Python logging 模块，易于扩展

**不足与改进建议:**

**(a) 缺少 trace_id / request_id 关联**
当前日志没有请求追踪 ID。在 `_poll()` 和 `_send_batch()` 等异步流程中，一次 RPC 查询到推送的完整链路无法关联。建议：
- 引入 `contextvars.ContextVar` 存储当前 trace_id
- 在 FastAPI middleware 中为每个 HTTP 请求生成 trace_id
- 在 ChainListener._poll() 入口生成 span_id，传递给 pusher 和 recorder
- 在 JSONFormatter 中自动附加 trace_id/span_id

**(b) 缺少日志采样和分级存储**
当前所有日志统一走 stdout。对于高吞吐场景（如 backtest 批量回放），DEBUG 日志会淹没关键信息。建议：
- 对高频路径（_poll 循环）使用 DEBUG + 采样率控制（如每 100 次 poll 记录 1 次 debug 统计）
- 生产环境建议 log level 设为 WARNING 以上，详细日志写入独立文件
- 考虑集成结构化日志聚合（Loki/EFK stack）

**(c) 缺少性能指标埋点**
`base.py:_poll()` 中已有 `duration_ms` 计算但仅用于 logger extra，未形成时序指标。需要 Prometheus Counter/Histogram 埋点。

### 1.2 Prometheus Metrics 集成方案

**推荐依赖**: `prometheus-fastapi-instrumentator` + `prometheus-client`

**核心指标设计:**

```
# RPC 层指标
evm_rpc_requests_total{chain, node_url, method, status}        # Counter
evm_rpc_latency_seconds{chain, method}                          # Histogram ( buckets: 0.1, 0.5, 1, 2, 5, 10)
evm_rpc_errors_total{chain, node_url, error_type}               # Counter
evm_pool_active_nodes{chain}                                    # Gauge
evm_pool_failed_nodes{chain}                                   # Gauge

# 链监听层指标
evm_listener_polls_total{chain, status}                         # Counter ("success"/"error")
evm_listener_logs_received_total{chain}                         # Counter
evm_listener_block_lag{chain}                                  # Gauge (current_head - last_processed)

# Pusher 层指标
evm_pusher_buffer_size                                          # Gauge
evm_pusher_batches_sent_total{chain, status}                    # Counter
evm_pusher_logs_sent_total{chain}                               # Counter
evm_pusher_retries_total{chain}                                 # Counter
evm_pusher_send_latency_seconds{chain}                          # Histogram

# Cache 层指标
evm_cache_size                                                  # Gauge
evm_cache_dropped_total                                         # Counter
evm_cache_hits_total                                            # Counter
evm_cache_misses_total                                          # Counter

# 应用层指标
evm_uptime_seconds                                              # Counter
evm_mode_info                                                   # Gauge (realtime=1/backtest=2)
```

**实现位置:**
- `/metrics` 端点: 在 `main.py` 中添加 `prometheus_client.generate_latest()` 路由
- RPC 指标: 在 `apipool_client.py` 的 `get_block_number()` / `get_logs()` / `raw_call()` 中包装
- 链监听指标: 在 `base.py:_poll()` 的入口和出口处埋点
- Pusher 指标: 在 `pusher.py:_do_post_with_retry()` 和 `_flush_all()` 中埋点
- Cache 指标: 在 `log_cache.py:add()` / `add_many()` / `query()` 中埋点

### 1.3 分布式追踪（OpenTelemetry）

**必要性评估:** 中等。系统架构相对简单（单进程 + 异步），核心调用链为：

```
ChainListener._poll() → EvmRpcPool.get_logs() → JsonRpcClient.__call__() → HTTP POST to RPC Node
                                                              ↓
ChainListener.log_callback() → LogCache.add() + LogPusher.on_new_logs()
                                                             → LogPusher._flush_all() → _do_post → AlertProcessor
```

**轻量方案（推荐）:**
```python
# 不引入完整 OTel SDK，改用结构化 span 日志 + 手动传播
from contextvars import ContextVar

trace_ctx: ContextVar[dict] = ContextVar("trace_context", default={})

# 在 _poll 入口:
trace_ctx.set({"trace_id": uuid4().hex[:16], "span_id": uuid4().hex[:8], "start_time": time.time()})
# 自动通过 logger.extra 传播，pusher/recorder 从 closure 或参数继承
```

**完整 OTel 方案（如果未来微服务化）:**
```python
# requirements.txt 添加:
# opentelemetry-api>=1.24.0
# opentelemetry-sdk>=1.24.0
# opentelemetry-instrumentation-aiohttp-client>=0.43b0
# opentelemetry-exporter-otlp>=1.24.0
# opentelemetry-instrumentation-fastapi>=0.43b0
```

### 1.4 健康检查增强

**现状**: `/health` 返回 chain status + rpc_nodes + pusher info；`/ready` 返回基本检查项。

**增强建议:**

| 端点 | 新增检查 | 说明 |
|------|---------|------|
| `/health` | cache_health | 检查缓存使用率是否 > 90% |
| `/health` | db_recorder_health | SQLite 连接可用性 + WAL mode 状态 |
| `/health` | block_lag_alert | 任一链 lag > N 个块（可配置阈值）时标记 degraded |
| `/health/live` | 进程存活 | 最小化 liveness probe |
| `/ready` | downstream_connectivity | AlertProcessor 可达性检查 |
| `/metrics` | Prometheus 抓取 | 标准指标端点 |

**代码修改要点 (`main.py:650-717`):**
- 将 health_check 拆分为浅层 `/health/live`（仅进程状态）和深层 `/health/ready`（依赖就绪）
- 增加 `cache_utilization = len(_cache) / _cache.max_size`
- 对每个 recorder 执行 `SELECT 1` 验证 SQLite 连接
- 计算 `block_lag = head_block - last_block` 并设置阈值告警

---

## 2. 告警与监控

### 2.1 关键监控指标

**P0 — 立即响应（电话/短信）:**

| 指标 | 告警条件 | 含义 |
|------|---------|------|
| evm_listener_polls_total{status="error"} rate | > 0 持续 5 分钟 | 所有 RPC 节点不可用 |
| evm_pusher_batches_sent_total{status="failed"} rate | > 0 持续 10 分钟 | AlertProcessor 完全不可达 |
| process_uptime < 300s | 反复重启 | 进程崩溃循环 |

**P1 — 30分钟内响应（IM/工单）:**

| 指标 | 告警条件 | 含义 |
|------|---------|------|
| evm_listener_block_lag{chain} | > 阈值（如 eth=12块, bsc=20块） | 监听落后于链头 |
| evm_rpc_latency_seconds p99 | > 10s | RPC 严重延迟 |
| evm_pusher_retries_total rate | > 5/min 持续 5min | 下游不稳定 |
| evm_cache_dropped_total rate | > 0 持续 10min | 缓存溢出丢数据 |
| evm_pool_failed_nodes{chain} / total | > 50% | 多数节点异常 |

**P2 — 每日巡检:**

| 指标 | 告警条件 | 含义 |
|------|---------|------|
| evm_pusher_buffer_size | 持续增长不下降 | 推送积压 |
| disk_usage recordings/ | > 80% | SQLite 文件占满磁盘 |
| sqlite_wal_size | > 100MB | WAL 未及时 checkpoint |

### 2.2 告警规则示例 (Prometheus Alertmanager)

```yaml
groups:
  - name: evm-listener-critical
    rules:
      - alert: ListenerAllRPCNodesDown
        expr: increase(evm_listener_polls_total{status="error"}[5m]) > 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "All RPC nodes failed for chain {{ $labels.chain }}"
          
      - alert: PusherDownstreamUnavailable
        expr: increase(evm_pusher_batches_sent_total{status="failed"}[10m]) > 0
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "AlertProcessor unreachable for {{ $labels.chain }}"

      - alert: HighBlockLag
        expr: evm_listener_block_lag > 12
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.chain }} lagging {{ $value }} blocks behind head"
          
      - alert: CacheDroppingLogs
        expr: rate(evm_cache_dropped_total[10m]) > 0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Cache dropping logs at rate {{ $value | humanize }}"
          
      - alert: RPCHighLatency
        expr: histogram_quantile(0.99, sum(rate(evm_rpc_latency_seconds_bucket[5m])) by (le, chain)) > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "RPC p99 latency {{ $value }}s for {{ $labels.chain }}"
```

### 2.3 Grafana Dashboard 设计

建议创建 4 个 Panel 组:

**Panel 1 — Overview (单屏总览):**
- Row 1: 应用运行时间、模式、总链数、健康状态（红绿灯）
- Row 2: 各链 block lag 时序图（折线图，叠加阈值线）
- Row 3: 每秒接收日志量（stacked area by chain）
- Row 4: Pusher 发送成功率 + 重试次数

**Panel 2 — RPC Deep Dive:**
- 各节点请求量（bar chart by node_url）
- RPC 延迟分布（heatmap by chain + method）
- 失败节点列表（table with status）
- Pool 活跃/失败/归档节点计数

**Panel 3 — Pipeline Health:**
- Cache 使用率 gauge + 丢弃计数
- Buffer 大小时序图
- Recorder 写入速率 + DB 大小趋势
- 端到端延迟（poll receive → push sent）

**Panel 4 — Error Analysis:**
- 错误分类饼图（rate_limit / timeout / connection / server_error）
- 最近错误日志表格（loki 数据源）
- Retry 成功率趋势

---

## 3. 部署优化

### 3.1 Dockerfile 改进

**当前问题:**
- 单阶段构建，镜像包含 pip 缓存和构建产物
- 以 root 用户运行
- 没有 .dockerignore
- COPY 整个 src 目录，缺乏精确性
- CMD 直接用 python -m 而非 uvicorn 可执行文件

**改进版 Dockerfile:**

```dockerfile
# ============ Build Stage ============
FROM python:3.10-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============ Runtime Stage ============
FROM python:3.10-slim AS runtime

LABEL maintainer="devops-team" \
      description="EVM Chain Listener" \
      version="1.0.0"

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY config.yaml.example /app/config.yaml

RUN mkdir -p /app/recordings && chown -R appuser:appuser /app

USER appuser

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ready')"]

CMD ["uvicorn", "evm_chain_listener.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-config", "/dev/null"]
```

**预期效果:** 镜像从约 150MB 降至约 70-80MB。

**新增 .dockerignore:**
```
.git
__pycache__
*.pyc
.codebuddy
*.md
.env*
recordings/*.db
tests/
.pytest_cache
*.egg-info
dist/
build/
```

### 3.2 docker-compose 扩展

**当前问题:** 仅包含单个服务，缺少监控和辅助组件。

**扩展版 docker-compose.yaml:**

```yaml
version: '3.8'

services:
  evm-listener:
    build: .
    environment:
      - ALCHEMY_ETH_URL=${ALCHEMY_ETH_URL}
      - INFURA_ETH_URL=${INFURA_ETH_URL}
      - BSC_RPC_URL=${BSC_RPC_URL}
      - POLYGON_RPC_URL=${POLYGON_RPC_URL}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - listener-data:/app/recordings
    ports:
      - "8080:8080"
    restart: unless-stopped
    depends_on: []
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '1.0'
        reservations:
          memory: 256M
          cpus: '0.5'
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ready')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

  # 可选: Prometheus 指标采集
  prometheus:
    image: prom/prometheus:v2.50.0
    ports:
      - "9090:9090"
    volumes:
      - ./deploy/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=7d'

  # 可选: Grafana Dashboard
  grafana:
    image: grafana/grafana:10.3.0
    ports:
      - "3000:3000"
    volumes:
      - ./deploy/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
      - ./deploy/grafana/datasources:/etc/grafana/provisioning/datasources:ro
      - grafana-data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-admin}
      - GF_USERS_ALLOW_SIGN_UP=false
    depends_on:
      - prometheus

volumes:
  listener-data:
  prometheus-data:
  grafana-data:
```

### 3.3 Kubernetes 部署清单

**Deployment 核心配置:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: evm-listener
spec:
  replicas: 1  # 初始单实例，见高可用性分析
  selector:
    matchLabels:
      app: evm-listener
  template:
    metadata:
      labels:
        app: evm-listener
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
      - name: listener
        image: evm-listener:1.0.0
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef:
            name: evm-listener-config
        - secretRef:
            name: evm-listener-secrets
        volumeMounts:
        - name: config-volume
          mountPath: /app/config.yaml
          subPath: config.yaml
        - name: recordings
          mountPath: /app/recordings
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /health/live
            port: 8080
          initialDelaySeconds: 15
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 10
      volumes:
      - name: config-volume
        configMap:
          name: evm-listener-config
      - name: recordings
        persistentVolumeClaim:
          claimName: recordings-pvc
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: evm-listener-config
data:
  config.yaml: |
    chains: ...
    # 完整配置内嵌或引用
---
apiVersion: v1
kind: Secret
metadata:
  name: evm-listener-secrets
type: Opaque
stringData:
  ALCHEMY_ETH_URL: "${ALCHEMY_ETH_URL}"
  INFURA_ETH_URL: "${INFURA_ETH_URL}"
```

### 3.4 配置管理策略

| 配置项 | 类型 | 管理方式 |
|--------|------|----------|
| chains / cache / api | 非敏感 | ConfigMap |
| RPC URLs (含 API Key) | 敏感 | Secret (K8s) / env_file (Docker) |
| alert_processor.url | 可能敏感 | Secret |
| log.level / format | 环境差异化 | ConfigMap (按 namespace 分) |
| recorder.directory | 存储路径 | PVC mount |

---

## 4. 高可用性

### 4.1 单点故障分析

| 组件 | SPOF? | 影响 | 缓解措施 |
|------|-------|------|----------|
| 应用进程 | 是 | 全部监听中断 | K8s Deployment restart + PodDisruptionBudget |
| 本地 SQLite (recorder) | 是 | 录制数据丢失 | PVC + 定期备份到对象存储 |
| LogCache (内存) | 是 | 缓存数据丢失（可接受） | 内存缓存天然 ephemeral，无需特殊处理 |
| apipool-server | 否（可选） | 降级为静态 URL 模式 | EvmRpcPool 有本地 URL fallback |
| AlertProcessor | 是（下游） | 推送堆积+重试 | pusher 内置 retry + buffer + reconnect gap fill |
| 单机宿主机 | 是 | 全部服务不可用 | K8s 多节点集群 / 跨 AZ 分布 |

### 4.2 水平扩展可行性评估

**无状态组件（可直接水平扩展）:**
- FastAPI HTTP 服务层（`/api/logs`, `/api/chains`, `/api/pusher/stats` 等 API 只读）
- BacktestRunner（每个任务独立，但需协调避免重复提交）

**有状态组件（水平扩展受限）:**
- **ChainListener**: 同一条链不能同时跑多个实例，否则会产生重复日志消费。解决方案：Kafka 分区消费或 leader election（使用 Kubernetes Leader Election 或者 redis 分布式锁）
- **LogPusher**: 如果多实例同时 push，下游 AlertProcessor 需要幂等处理（去重 key: chain_id+block_number+tx_hash+log_index）。当前 pusher 无去重机制。
- **LogDbRecorder**: 多实例同时 write 同一个 SQLite 会产生锁冲突。SQLite 天然不支持并发写。

**扩展建议:**
- **短期（单实例高可用）**: 保持 replicas=1，用 PDB 防止自愿中断，配合快速重启（liveness probe）
- **中期（链级分区）**: 不同 chain 分配不同 Pod（chain ethereum → pod-a, chain bsc → pod-b），通过 configmap 切片部署
- **长期（完整无状态化）**: 替换 SQLite 为 PostgreSQL（recorder），替换内存缓存为 Redis（cache），引入消息队列做 log buffer

### 4.3 故障转移机制

**RPC 层（已有较好支持）:**
- `apipool-ng AsyncDynamicKeyManager` 已实现节点轮换、ban、自动刷新
- `EvmRpcPool.from_server()` 模式下，apipool-server 动态管理节点列表
- 本地模式下 `ApiKeyManager` 提供基础 failover
- **不足**: 无跨区域/跨云 provider RPC 池。建议至少配置每个 chain 包含 2 个不同 provider 的 URL（Alchemy + Infura）

**应用层故障恢复（当前能力）:**

| 故障场景 | 当前行为 | 改进建议 |
|----------|---------|---------|
| RPC 全部超时 | AllNodesFailedError → sleep poll_interval 后重试 | 已合理；可增加指数退避上限封顶 |
| AlertProcessor 不可达 | retry 3次 + 指数退避，数据保留在 buffer | 合理；增加 dead letter queue（本地文件持久化 buffer） |
| 应用崩溃 | 数据丢失（内存 cache + 未 flush buffer） | 增加优雅关闭超时强制 flush（SIGTERM handler） |
| 磁盘满 | SQLite 写入异常 → RuntimeError | 增加 pre-write 磁盘空间检查 + 自动清理最旧 recording |
| SQLite WAL 过大 | 性能下降 | 定时 PRAGMA wal_checkpoint(TRUNCATE) |

**新增建议 — 优雅关闭增强 (`main.py:lifespan shutdown`):**
```python
# 当前: await _pusher.stop() 会 flush 一次
# 建议: 增加全局超时 + 强制 flush 保证
async def shutdown_handler():
    shutdown_timeout = 30  # 秒
    try:
        await asyncio.wait_for(_pusher.stop(), timeout=shutdown_timeout)
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown timed out, forcing exit")
```

### 4.4 备份恢复策略

**SQLite Recording 备份:**

```bash
# 方案 1: 定期 .backup() 到冷存储（不影响写入）
sqlite3 recordings/logs.db ".backup '/backups/logs_$(date +%Y%m%d_%H%M%S).db'"

# 方案 2: 利用 WAL 模式的特性 — 直接复制数据库文件（一致性快照）
# 需先执行: PRAGMA wal_checkpoint(TRUNCATE);

# Cron job 示例（每小时增量备份 + 每日全量归档）
# K8s: CronJob + sidecar init-container 做备份
# Docker: host volume + host cron
```

**恢复流程:**
1. 部署新实例，挂载 backup PVC
2. 配置 replay.file_path 指向备份数据库
3. 以 backtest 模式启动，自动回放到最新 block
4. 切换回 realtime 模式，从 replay 最后 block 继续

---

## 5. 运维工具

### 5.1 管理 CLI 工具需求

建议基于 Click 或 Typer 实现 `evm-cli` 子命令:

| 命令 | 功能 | 说明 |
|------|------|------|
| `evm-cli status` | 查询各链监听状态 | GET /api/chains + /health |
| `evm-cli stats` | 查看缓存/pusher 统计 | GET /api/logs/stats + /api/pusher/stats |
| `evm-cli logs query` | 查询日志 | GET /api/logs with filters |
| `evm-cli backtest submit` | 提交回测任务 | POST /api/backtest/run |
| `evm-cli backtest list` | 列出回测任务 | GET /api/backtest/tasks |
| `evm-cli replay start` | 启动回放 | POST /api/replay/start |
| `evm-cli recordings list` | 列出录制文件 | GET /api/replay/recordings |
| `evm-cli config validate` | 校验配置文件合法性 | 加载 config.yaml + schema check |
| `evm-cli export-metrics` | 输出当前指标快照 | GET /metrics 解析输出 |

**最小可行版本:** 先实现 `status` 和 `logs query` 两个高频命令。

### 5.2 日志轮转策略

**容器环境（stdout → Docker log driver / cluster logging agent）:**

```dockercompose
# docker-compose 中添加 log 配置
services:
  evm-listener:
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"     # 保留 5 个 × 50MB = 250MB 上限
        compress: "true"
```

**非容器 / file output 场合（`config.yaml` 中 `log.output: file`）:**

当前 `setup_logging()` 创建 FileHandler 但无轮转。建议改为:

```python
# utils/logging.py 修改
from logging.handlers import RotatingFileHandler  # 或 TimedRotatingFileHandler

if output == "file":
    handler = RotatingFileHandler(
        file_path,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=5,
        encoding="utf-8",
    )
```

**生产推荐方案:**
- stdout 输出 JSON 格式日志
- 容器日志 driver 做 size-based 轮转
- Fluentd / Vector 侧car 收集 → Loki/Elasticsearch
- Loki 保留策略: 30天热存储 + S3 冷归档

### 5.3 性能剖析工具集成

**(a) 应用内置 profiler（开发/调试）:**

```python
# 新增 /debug/pprof 端点（可选启用）
# 依赖: yappi (协程感知) 或 cProfile

@app.get("/debug/profile", include_in_schema=False)
async def profile(duration_sec: int = 10):
    """Run CPU profiler for duration seconds, return pprof-format result."""
    import yappi
    yappi.set_clock_type("wall")
    yappi.start()
    await asyncio.sleep(duration_sec)
    yappi.stop()
    # 返回 pprof 格式供 go tool pprof 可视化
    ...
```

**(b) 外部观测:**

| 工具 | 用途 | 集成方式 |
|------|------|---------|
| `py-spy` | 生产环境低开销采样 profiling | `py-spy record --pid $(pid evm_listener) -o profile.svg` |
| `memray` | 内存分配追踪 | `memray run -m evm_chain_listener.main` |
| `aiometer` | aiohttp 并发度量 | 作为 dependency 注入到 ClientSession |
| `fastapi-profiler` | 自动 endpoint 级别耗时统计 | middleware 注入 |

**(c) cProfile 集成到 backtest 模式（推荐优先实现）:**

BacktestRunner 是 CPU 密集型场景（批量查询历史日志），最容易遇到性能瓶颈：

```python
# chains/backtest.py 中添加可选 profiler
import cProfile
import pstats
import io

if self._enable_profiler:
    pr = cProfile.Profile()
    pr.enable()
    # ... 执行查询 ...
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(20)
    logger.info(f"[profiler]\n{s.getvalue()}")
```

---

## 6. 改进优先级路线图

### Phase 1 — 快速见效（1-2 周）
1. Dockerfile 多阶段构建 + 非 root 用户 + HEALTHCHECK
2. 添加 `.dockerignore`
3. 日志轮转（RotatingFileHandler）
4. `/metrics` 端点 + 核心指标埋点（Counter: polls_total, pusher_batches, errors_total）
5. 增强 `/health` 端点（cache utilization + block lag）
6. docker-compose 添加 resource limits + healthcheck

### Phase 2 — 监控闭环（2-4 周）
7. Prometheus + Grafana compose 服务
8. Alertmanager 规则（P0/P1 告警）
9. Grafana Dashboard（Overview + RPC Deep Dive）
10. 优雅关闭增强（SIGTERM handler + 强制 flush timeout）
11. trace_id 上下文传播（ContextVar 方案）
12. 管理 CLI: `evm-cli status` + `evm-cli logs query`

### Phase 3 — 高可用（1-2 月）
13. K8s Deployment + ConfigMap/Secret 清单
14. PVC for recordings + 自动备份 CronJob
15. 磁盘空间监控 + 自动清理
16. SQLite WAL checkpoint 定时任务
17. Dead Letter Queue（pusher buffer 持久化到本地文件）

### Phase 4 — 扩展性准备（长期）
18. Recorder PostgreSQL migration path 设计
19. Redis cache 替代内存 LogCache 方案
20. 消息队列集成（日志缓冲解耦）
21. OpenTelemetry 完整接入
