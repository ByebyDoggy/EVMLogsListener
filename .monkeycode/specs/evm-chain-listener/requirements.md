# EVM Chain Listener 需求文档

## 引言

EVM Chain Listener是一个轻量级的后台服务，用于监控EVM兼容链的区块日志事件，通过多RPC节点轮询机制获取区块日志信息，并支持Webhook实时推送。

## 词汇表

- **EVM**: Ethereum Virtual Machine，以太坊虚拟机
- **RPC**: Remote Procedure Call，远程过程调用
- **Chain ID**: EIP-155定义的链ID，用于区分不同区块链网络
- **Webhook**: HTTP POST回调机制
- **Rate Limit**: API速率限制
- **Log Filter Too Large**: RPC节点对单次查询的日志数量限制

## 需求

### 需求1：多链监听

**用户故事：** AS 开发者，我想要 同时监听多条EVM兼容链，以便 统一处理不同链上的日志事件

#### Acceptance Criteria

1. WHEN 服务启动，THEN 系统 SHALL 支持配置多个不同的EVM兼容链
2. WHEN 每条链 SHALL 独立运行轮询任务，互相不干扰
3. WHEN 每条链 SHALL 独立维护区块进度状态
4. WHEN 系统 SHALL 支持的链包括：Ethereum、BNB Chain、Polygon、Arbitrum、Optimism

### 需求2：RPC节点池管理

**用户故事：** AS 系统，我需要 管理多个RPC节点，以便 单节点故障时自动切换保证服务连续性

#### Acceptance Criteria

1. WHEN 每条链 SHALL 配置多个RPC节点URL
2. WHEN 每个节点 SHALL 定义优先级（数字越小优先级越高）
3. WHEN 每个节点 SHALL 支持速率限制配置（requests_per_second, burst）
4. WHEN 系统 SHALL 支持环境变量在URL中（${VAR_NAME}格式）
5. WHEN 请求 SHALL 按优先级从高到低尝试可用节点
6. WHEN 当前节点失败时，THEN 系统 SHALL 自动切换到下一个节点
7. WHEN 所有节点不可用时，THEN 系统 SHALL 记录错误并等待后重试

### 需求3：轮询与日志查询

**用户故事：** AS 系统，我需要 定时轮询新区块日志，以便 及时获取链上事件

#### Acceptance Criteria

1. WHEN 系统 SHALL 支持可配置的轮询间隔（15秒/30秒/60秒）
2. WHEN 轮询 SHALL 查询自上次成功查询区块之后的所有新区块
3. WHEN eth_getLogs 请求 SHALL 支持 fromBlock、toBlock、address、topics 参数
4. WHEN 首次启动时，THEN 系统 SHALL 从当前区块开始监听（不查询历史）
5. WHEN 查询成功时，THEN 系统 SHALL 更新本地区块高度进度

### 需求4：速率限制处理

**用户故事：** AS 系统，我需要 处理RPC节点的速率限制，以便 避免被封禁或限流

#### Acceptance Criteria

1. WHEN RPC返回429 Rate Limit错误，THEN 系统 SHALL 切换到下一个节点
2. WHEN 所有节点都触发速率限制时，THEN 系统 SHALL 指数退避等待后重试
3. WHEN 退避等待时间 SHALL 从1秒开始，最大32秒
4. WHEN 速率限制计数器 SHALL 按节点独立维护

### 需求5：日志数量限制与二分法补全

**用户故事：** AS 系统，我需要 处理RPC节点的日志数量限制，以便 完整获取大区间内的所有日志

#### Acceptance Criteria

1. WHEN RPC返回-32005 Log filter too large错误，THEN 系统 SHALL 使用二分法拆分查询区间
2. WHEN 二分法 SHALL 计算区间中点，将查询拆分为两个子区间
3. WHEN 子区间查询 SHALL 递归应用相同逻辑
4. WHEN 当区间缩小到单区块仍超限时，THEN 系统 SHALL 返回空日志列表并记录警告
5. WHEN 二分法 SHALL 确保不遗漏任何日志

### 需求6：网络错误处理

**用户故事：** AS 系统，我需要 处理网络异常，以便 保证服务的鲁棒性

#### Acceptance Criteria

1. WHEN 连接超时时，THEN 系统 SHALL 切换节点并立即重试
2. WHEN 收到无效响应时，THEN 系统 SHALL 切换节点并记录错误
3. WHEN 发生任何网络异常时，THEN 系统 SHALL 记录详细错误日志（包含错误类型、节点URL、请求参数）
4. WHEN 网络错误 SHALL 触发节点健康检查，THEN 连续失败3次的节点 SHALL 被标记为不健康

### 需求7：Webhook通知

**用户故事：** AS 开发者，我想要 实时接收日志事件，以便 在我的应用中处理链上事件

#### Acceptance Criteria

1. WHEN 系统 SHALL 支持配置多个Webhook endpoint
2. WHEN 获取到日志时，THEN 系统 SHALL 发送HTTP POST请求到所有配置的endpoint
3. WHEN 请求 SHALL 包含JSON格式的日志数据
4. WHEN 请求 SHALL 支持自定义Headers（包括Authorization）
5. WHEN 请求超时 SHALL 可配置（默认10秒）
6. WHEN 发送失败时，THEN 系统 SHALL 指数退避重试（默认3次，最大等待32秒）
7. WHEN 包含X-Chain-ID header以便接收方区分链

### 需求8：健康检查

**用户故事：** AS 运维人员，我需要 健康检查接口，以便 监控服务状态

#### Acceptance Criteria

1. WHEN health.enabled=true时，THEN 系统 SHALL 启动HTTP健康检查服务
2. WHEN GET /health请求 SHALL 返回所有链的监听状态
3. WHEN 响应 SHALL 包含最后成功查询的区块高度
4. WHEN 响应 SHALL 包含当前活跃的RPC节点信息

### 需求9：配置管理

**用户故事：** AS 运维人员，我需要 YAML配置文件管理所有设置，以便 灵活部署和配置

#### Acceptance Criteria

1. WHEN 系统 SHALL 从config.yaml文件读取所有配置
2. WHEN 配置 SHALL 支持环境变量插值（${VAR_NAME}格式）
3. WHEN 缺少必需配置时，THEN 系统 SHALL 输出明确错误信息并退出
4. WHEN 配置文件 SHALL 支持日志级别设置（DEBUG/INFO/WARNING/ERROR）
5. WHEN 日志输出 SHALL 支持JSON格式

### 需求10：日志缓存

**用户故事：** AS 开发者，我需要 将获取的日志缓存在本地内存，以便 外部系统通过查询接口获取日志

#### Acceptance Criteria

1. WHEN 系统 SHALL 使用内存缓存存储获取到的日志
2. WHEN 缓存 SHALL 使用FIFO（先进先出）策略
3. WHEN 缓存大小 SHALL 可配置（默认max_size=10000条日志）
4. WHEN 当缓存达到max_size时，THEN 系统 SHALL 自动清除最旧的日志
5. WHEN 缓存 SHALL 支持按链ID过滤查询
6. WHEN 缓存 SHALL 支持按时间范围过滤查询
7. WHEN 缓存 SHALL 支持分页查询
8. WHEN 系统 SHALL 提供HTTP查询接口获取缓存的日志
9. WHEN 日志 SHALL 支持按 log_index 或 transaction_hash 去重

### 需求11：优雅关闭

**用户故事：** AS 系统，我需要 优雅关闭机制，以便 避免中断处理中的请求

#### Acceptance Criteria

1. WHEN 收到SIGTERM/SIGINT信号时，THEN 系统 SHALL 停止接收新请求
2. WHEN 关闭前 SHALL 等待当前正在处理的请求完成
3. WHEN 关闭前 SHALL 保存当前区块进度状态到日志
4. WHEN 关闭时 SHALL 关闭所有RPC连接和Webhook连接
