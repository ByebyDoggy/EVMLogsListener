# apipool-ng v1.0.4 迁移指南

> 从旧版本平滑升级到 v1.0.4 的完整指南。本文档以 `EVMLogListener` 项目中的 `apipool_client.py` 为参照案例进行分析。

---

## 目录

1. [版本概述](#1-版本概述)
2. [核心差异总览](#2-核心差异总览)
3. [新增功能详解](#3-新增功能详解)
4. [废弃/变更接口说明](#4-废弃变更接口说明)
5. [代码重构步骤（分步指南）](#5-代码重构步骤分步指南)
6. [EVMLogListener 迁移对照表](#6-evmloglistener-迁移对照表)
7. [常见问题 FAQ](#7-常见问题-faq)

---

## 1. 版本概述

| 维度 | 旧版 (< 1.0.4) | v1.0.4 |
|---|---|---|
| **异步支持** | 仅同步 ChainProxy/DummyClient | 新增 AsyncChainProxy/AsyncDummyClient/AsyncApiCaller |
| **ApiKey 基类** | 同步 `connect_client()` / `is_usable()` | 新增 `aconnect_client()` / `ais_usable()` |
| **服务端模式** | 无 | 新增 `connect()` / `async_connect()` / `login()` / `alogin()` / `get_keys()` / `aget_keys()` |
| **客户端 HTTP 库** | 用户自选（如 aiohttp） | SDK 层统一使用 httpx |
| **内部属性访问** | 直接访问 `_client`、`_manager.apikey_chain` 等 | 不变（向后兼容） |

---

## 2. 核心差异总览

### 2.1 导入路径变化

```python
# ====== 旧版本导入 ======
from apipool import ApiKey, ApiKeyManager, PoolExhaustedError

# ====== v1.0.4 完整导入（推荐全部显式导入）======
from apipool import (
    # 核心类（不变）
    ApiKey,
    ApiKeyManager,
    PoolExhaustedError,
    # 统计模块（不变）
    StatusCollection,
    StatsCollector,
    # === 新增：异步支持 ===
    AsyncApiCaller,
    AsyncChainProxy,
    AsyncDummyClient,
    # === 新增：SDK 服务端模式 ===
    connect,          # sync connect
    login,            # sync login
    get_keys,         # sync get raw keys
    async_connect,    # async connect
    alogin,           # async login
    aget_keys,        # async get raw keys
)
```

### 2.2 ApiKey 基类变更

```
ApiKey (v1.0.x)                          ApiKey (v1.0.4)
├── get_primary_key()                     ├── get_primary_key()         [不变]
├── create_client()                       ├── create_client()           [不变]
├── test_usability(client)                ├── test_usability(client)     [不变]
├── primary_key                           ├── primary_key               [不变]
├── connect_client()                      ├── connect_client()           [不变]
│                                        ├── aconnect_client()        [新增] 异步连接
└── is_usable()                           └── ais_usable()             [新增] 异步可用性检测
   └── is_usable()                         └── is_usable()              [不变]
```

**关键点**：
- 所有旧方法签名完全不变，**无需修改现有子类即可升级**
- 新增的 `aconnect_client()` 和 `ais_usable()` 是可选增强，不影响现有代码

### 2.3 ApiKeyManager 变更

```
ApiKeyManager (v1.0.x)                   ApiKeyManager (v1.0.4)
├__init__(apikey_list, reach_limit_exc)  ├ __init__(...)                [不变]
├ add_one(apikey, upsert=False)          ├ add_one(...)                 [不变，内部增加 _client_connected 检测]
├ fetch_one(primary_key)                 ├ fetch_one(...)               [不变]
├ remove_one(primary_key)                ├ remove_one(...)              [不变]
├ random_one()                           ├ random_one()                 [不变]
├ check_usable()                         ├ check_usable()               [不变]
├ stats                                  ├ stats                        [不变]
├ dummyclient                            ├ dummyclient                  [不变]
│                                       ├ adummyclient                 [新增] 异步入口
└ archived_apikey_chain                  └ archived_apikey_chain        [不变]
```

---

## 3. 新增功能详解

### 3.1 完整的异步链式调用系统

v1.0.4 引入与同步系统完全平行的异步调用架构：

| 同步类 | 异步类（新增） | 说明 |
|---|---|---|
| `ChainProxy` | `AsyncChainProxy` | 属性链导航，`__call__` 为 `async` 方法 |
| `ApiCaller` | `AsyncApiCaller` | 调用执行器，自动 `await` 协程结果 |
| `DummyClient` | `AsyncDummyClient` | 异步链式调用入口 |

**核心机制**：`AsyncApiCaller.__call__` 使用 `inspect.isawaitable()` 自动检测目标方法是否返回协程，如果是则 `await`，否则直接返回。这意味着：

- 真正的异步方法（返回 coroutine）：自动 await
- 同步方法被错误放入异步路径：也能正常工作

**使用示例**：

```python
from apipool import ApiKey, ApiKeyManager, AsyncDummyClient

# 你的异步 ApiKey 子类
class MyAsyncApiKey(ApiKey):
    def __init__(self, key: str):
        self._key = key

    def get_primary_key(self):
        return self._key

    def create_client(self):
        return MyAsyncSDK(self._key)

    def test_usability(self, client):
        return client.ping()  # 可以是协程或普通值

# 构建管理器
keys = ["key1", "key2"]
manager = ApiKeyManager([MyAsyncApiKey(k) for k in keys])

# 使用异步链式调用
result = await manager.adummyclient.some_method(param1, param2)
result = await manager.adummyclient.a.b.c.nested_call()
```

### 3.2 服务端代理模式（Server Proxy Mode）

全新引入的服务端 SDK 模式，将 apipool-server 作为**密钥保险柜**使用。

#### 工作流程

```
┌─────────────┐    login()     ┌─────────────────┐
│  你的应用    │ ─────────────→ │  apipool-server │
│             │ ← JWT Token ── │                 │
│             │                │                 │
│  get_keys() │ ─────────────→ │  解密 & 返回     │
│  本地构建    │ ← raw_keys[] ──│  原始密钥列表    │
│  ApiKeyManager│               │                 │
│  本地执行    │ ── API 调用 ──→ │  (不经过服务器)  │
└─────────────┘                └─────────────────┘
```

#### API 函数一览

| 函数 | 类型 | 用途 |
|---|---|---|
| `login(service_url, username, password)` → `dict` | sync | 获取 JWT token |
| `connect(service_url, pool_id, token)` → `ApiKeyManager` | sync | 连接服务端池（代理模式） |
| `get_keys(service_url, client_type, token)` → `list[str]` | sync | 获取解密后的原始密钥 |
| `alogin(...)` → `dict` | async | 异步登录 |
| `async_connect(...)` → `ApiKeyManager` | async | 异步连接服务端池 |
| `aget_keys(...)` → `list[str]` | async | 异步获取原始密钥 |

#### 使用示例：本地 SDK 模式（推荐）

```python
from apipool import login, get_keys, ApiKeyManager

# 1. 认证
tokens = login("http://localhost:8000", "alice", "password")

# 2. 获取原始密钥
raw_keys = get_keys(
    service_url="http://localhost:8000",
    client_type="ethereum-rpc",  # 你在服务端设置的类型标签
    auth_token=tokens["access_token"],
)
# raw_keys = ["https://node1.example.com", "https://node2.example.com", ...]

# 3. 封装成你自己的 ApiKey 子类 + 构建管理器
apikeys = [EthRpcApiKey(url) for url in raw_keys]
manager = ApiKeyManager(
    apikey_list=apikeys,
    reach_limit_exc=RpcRateLimitError,
)

# 4. 正常使用（API 调用在本地执行）
block_num = await manager.adummyclient.eth_block_number()
```

#### 使用示例：纯代理模式

```python
from apipool import login, async_connect

tokens = await alogin("http://localhost:8000", "alice", "pass")
manager = await async_connect(
    service_url="http://localhost:8000",
    pool_identifier="my-eth-pool",
    auth_token=tokens["access_token"],
)
# 所有调用通过服务器代理转发
result = await manager.adummyclient.eth_block_number()
```

### 3.3 ApiKey 基类异步扩展

```python
class ApiKey:
    # ... 原有方法不变 ...

    async def aconnect_client(self):
        """异步连接客户端。
        
        如果 create_client 是协程函数则 await，
        否则回退到同步 create_client。
        """
        if inspect.iscoroutinefunction(self.create_client):
            self._client = await self.create_client()
        else:
            self._client = self.create_client()

    async def ais_usable(self):
        """异步可用性检测。
        
        如果 test_usability 返回的是可等待对象则 await。
        """
        if self._client is None:
            await self.aconnect_client()
        try:
            result = self.test_usability(self._client)
            if inspect.isawaitable(result):
                return await result
            return result
        except:
            return False
```

---

## 4. 废弃/变更接口说明

### 4.1 无废弃接口

v1.0.4 **完全向后兼容**。所有旧版本的公开 API 均保持不变：

- `ApiKey.get_primary_key()` / `create_client()` / `test_usability()` ✅
- `ApiKey.connect_client()` / `is_usable()` ✅
- `ApiKeyManager.__init__()` / `add_one()` / `remove_one()` / `random_one()` / `check_usable()` ✅
- `PoolExhaustedError` ✅
- `StatsCollector` / `StatusCollection` ✅
- `DummyClient` / `ChainProxy` / `ApiCaller` ✅

### 4.2 内部行为微变

| 变更点 | 影响 | 需要处理？ |
|---|---|---|
| `add_one()` 内部增加了 `_client_connected` 标志位检查 | 如果你之前在 `add_one` 之前手动调用了 `aconnect_client()`，现在不会重复调用 `connect_client()` | 一般不需要处理 |
| `ApiKeyManager.__init__` 现在同时初始化 `self.adummyclient` | 多了一个属性，不影响现有代码 | 不需要 |

---

## 5. 代码重构步骤（分步指南）

以下以 `EVMLogListener` 的 `apipool_client.py` 为例，展示从当前版本迁移到 v1.0.4 的推荐步骤。

### Step 0: 升级依赖

```bash
pip install --upgrade apipool-ng>=1.0.4
```

### Step 1: 更新导入（最小改动）

```python
# ====== 当前代码 (apipool_client.py:28) ======
from apipool import ApiKey, ApiKeyManager, PoolExhaustedError

# ====== 迁移后（如果不需要新功能，这行可以不动）======
from apipool import ApiKey, ApiKeyManager, PoolExhaustedError
# ↑ 对纯库模式使用者来说，这一行完全不用改
```

### Step 2: 利用新的异步能力（推荐优化）

当前 `EvmRpcPool` 手动实现了一套异步调用逻辑（手动取 `random_one()` → 取 `_client` → 调用）。v1.0.4 提供了原生的 `adummyclient` 异步链式调用，可以简化代码。

**Before** (当前代码):

```python
# apipool_client.py:292-308 — 手动选取 key 并调用
async def get_block_number(self) -> int:
    try:
        apikey = self._manager.random_one()      # 手动选 key
        client = apikey._client                   # 手动取 client
        result = await client.eth_block_number()  # 手动调用
        ...
```

**After** (利用 adummyclient):

```python
# 利用 v1.0.4 的原生异步链式调用
async def get_block_number(self) -> int:
    try:
        result = await self._manager.adummyclient.eth_block_number()
        # adummyclient 内部自动完成：随机选择 key → 解析链 → 执行 → 统计
        if result is None:
            raise InvalidResponseError("eth_blockNumber returned null")
        return int(result, 16)
    except PoolExhaustedError:
        raise AllNodesFailedError(...)
```

### Step 3: 可选 — 切换到服务端密钥管理模式

如果你希望将 RPC URL 集中托管到 apipool-server：

```python
# ====== Before: URL 硬编码在本地 ======
urls = ["https://node1.example.com", "https://node2.example.com"]
pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="ethereum")

# ====== After: 从服务端动态获取 ======
from apipool import alogin, aget_keys

tokens = await alogin("http://your-apipool-server:8000", "user", "pass")
urls = await aget_keys(
    service_url="http://your-apipool-server:8000",
    client_type="ethereum-rpc",       # 在 Web UI 中创建 Key 时填写的类型
    auth_token=tokens["access_token"],
)
pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="ethereum")
# 后续代码完全不变
```

### Step 4: EthRpcApiKey 可选增强

如果你的 `JsonRpcClient` 未来改为异步初始化，可以利用 `aconnect_client()`:

```python
class EthRpcApiKey(ApiKey):
    def __init__(self, url: str, ...):
        # ... 不变 ...

    def create_client(self) -> JsonRpcClient:
        # ... 不变 ...

    # 新增：如果需要完全异步的客户端创建，可以覆盖此方法
    # async def create_client(self):  <-- 改为 async 即可
    #     session = aiohttp.ClientSession()
    #     return JsonRpcClient(session, self.url)
    
    # test_usability 在 v1.0.4 中可以被 ais_usable() 自动包装
    def test_usability(self, client) -> bool:
        # 现有代码完全兼容
        ...
```

---

## 6. EVMLogListener 迁移对照表

| 文件位置 | 当前实现 | v1.0.4 建议 | 改动量 |
|---|---|---|---|
| `:28` 导入 | `from apipool import ApiKey, ApiKeyManager, PoolExhaustedError` | 保持不变 | **无改动** |
| `:190` 类定义 | `class EthRpcApiKey(ApiKey)` | 保持不变 | **无改动** |
| `:206-230` 接口实现 | `get_primary_key` / `create_client` / `test_usability` | 保持不变 | **无改动** |
| `:216` `test_usability` | 手动处理 `asyncio.iscoroutine` | 可简化：基类 `ais_usable()` 已内置 `inspect.isawaitable()` | 可选优化 |
| `:241` 类定义 | `class EvmRpcPool` | 保持不变 | **无改动** |
| `:283` 初始化 | `ApiKeyManager(apikey_list, reach_limit_exc)` | 保持不变 | **无改动** |
| `:292-308` `get_block_number` | 手动 `random_one()` → `_client` → 调用 | 可改用 `await manager.adummyclient.xxx()` | **推荐优化** |
| `:330-356` `get_logs` | 手动 `random_one()` → `_client` → 调用 | 同上 | **推荐优化** |
| `:358-368` `raw_call` | 手动 `random_one()` → `getattr` → 调用 | 同上 | **推荐优化** |
| `:370-393` `health_check` | 遍历 `apikey_chain` + `archived_apikey_chain` | 保持不变（内部属性未改名） | **无改动** |
| `:408-411` `manager` 属性 | `@property → return self._manager` | 保持不变 | **无改动** |
| `:413-421` `dummy_client` 属性 | `return self._manager.dummyclient` | 可增加 `adummy_client` 属性 | 可选增强 |
| `:428-430` `check_usable` | 调用 `self._manager.check_usable()` | 保持不变 | **无改动** |

### 总结

**必须改动**: 无（100% 向后兼容）

**推荐优化**: 将手动 key 选择逻辑替换为 `adummyclient` 异步链式调用，减少约 30 行样板代码

---

## 7. 常见问题 FAQ

### Q1: 升级后现有的 `test_usability` 中手动处理协程的代码需要删除吗？

**不需要**。现有代码可以正常工作。但建议逐步迁移到基类提供的 `ais_usable()`，减少样板代码：

```python
# 旧写法（仍然有效）
def test_usability(self, client):
    result = client.eth_block_number()
    import asyncio
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()
    return result is not None

# 新写法（更简洁）— 直接让 ais_usable 处理
def test_usability(self, client):
    result = client.eth_block_number()
    # 返回值可以是普通值或协程，ais_usable 会自动处理
    return result is not None
```

### Q2: `adummyclient` 和 `dummyclient` 可以混用吗？

**可以**。同一个 `ApiKeyManager` 实例同时拥有两个入口：
- `manager.dummyclient.xxx()` — 同步调用（用于同步 SDK）
- `await manager.adummyclient.yyy()` — 异步调用（用于异步 SDK）

两者共享同一个 key 池和统计信息。

### Q3: 服务端模式的 `connect()` 和本地模式哪个更好？

取决于场景：

| 场景 | 推荐模式 | 原因 |
|---|---|---|
| 密钥需要在多个服务间共享 | 服务端 `connect()` | 集中管理，一处更新全局生效 |
| 低延迟、高频调用 | 本地 `ApiKeyManager` | API 调用不经服务器转发，延迟更低 |
| 密钥安全性要求极高 | 服务端 `get_keys()` | 原始密钥只在内存中短暂存在 |
| 离线环境 / 单机部署 | 本地 `ApiKeyManager` | 不依赖服务端可用性 |

### Q4: 从 v1.0.3 升级到 v1.0.4 有破坏性变更吗？

**没有**。v1.0.4 的所有新增内容都是增量式的：
- 新增类/函数不会覆盖已有名称
- 已有方法的签名和行为不变
- 内部数据结构 (`OrderedDict apikey_chain`) 未改变

### Q5: 如何验证升级是否成功？

```python
import apipool
print(apipool.__version__)  # 应输出 1.0.4

# 验证新增导出可用
from apipool import AsyncDummyClient, async_connect, alogin, aget_keys
print("All new imports OK")
```
