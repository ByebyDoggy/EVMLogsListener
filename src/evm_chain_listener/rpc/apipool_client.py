"""EVM RPC Client backed by apipool-ng **AsyncDynamicKeyManager** (v1.0.7+).

Provides:
  - ``EthRpcApiKey``: maps one RPC endpoint URL -> one :class:`~apipool.ApiKey`
  - ``EvmRpcPool``: high-level pool manager wrapping
    :class:`~apipool.AsyncDynamicKeyManager`, with the same public API as the
    original ``RPCNodePool``.
  - ``JsonRpcClient``: lightweight JSON-RPC client supporting attribute-chain
    navigation for use with ChainProxy.

Architecture (server-driven control)::

    +-------------------+       async refresh        +---------------+
    |   apipool-server  | <---------------------- | DynamicKeyMgr |
    |  - key list mgmt  |                            |  (this lib)  |
    |  - rotation strat |  - alogin / aget_keys      |              |
    |  - concurrency    |  - get_config / apply_cfg  |  adummyclient |
    |  - rate limiting  |                            |    .eth_xxx() |
    |  - key banning    |                            +------+-------+
    +-------------------+                                   |
                                                          v
                                                  +----------------+
                                                  | JsonRpcClient  |
                                                  | (HTTP transport)|
                                                  +----------------+

All node selection, retry, throttling, health-tracking, and failover is
delegated to ``AsyncDynamicKeyManager`` which syncs its behaviour from the
apipool-server at configurable intervals.  The client holds *zero* internal
polling / rotation / back-off state.

Usage (apipool-server auto-load — recommended)::

    pool = await EvmRpcPool.from_server(
        service_url="http://apipool-server:8000",
        pool_identifier="my-eth-pool",
        username="alice",
        password="secret",
        chain_id=1,
        chain_name="ethereum",
    )
    block_num = await pool.get_block_number()

Usage (local URLs — backward-compatible fallback)::

    urls = ["https://node1.example.com", "https://node2.example.com"]
    pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="ethereum")
    block_num = await pool.get_block_number()
"""

import json
import logging
import httpx
from typing import Any, Dict, List, Optional, Union

import aiohttp

from apipool import (
    ApiKey,
    AsyncDynamicKeyManager,
    PoolConfig,
    PoolExhaustedError,
    aget_config,
    aget_keys,
    alogin,
)

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class RpcRateLimitError(Exception):
    """Raised when an RPC node returns HTTP 429."""

    def __init__(self, *args, **kwargs):
        self.message = args[0] if args else ""
        self.node_url = kwargs.pop("node_url", "")
        super().__init__(*args)


class RpcTimeoutError(Exception):
    """Raised when an RPC request times out."""

    def __init__(self, *args, **kwargs):
        self.message = args[0] if args else ""
        self.node_url = kwargs.pop("node_url", "")
        super().__init__(*args)


class LogLimitExceededError(Exception):
    """Returned when eth_getLogs response is truncated."""
    pass


class AllNodesFailedError(Exception):
    """Every endpoint in the pool has been exhausted / failed."""
    pass


class InvalidResponseError(Exception):
    """Malformed response from RPC node."""

    def __init__(self, message, *args, **kwargs):
        self.node_url = kwargs.pop("node_url", "")
        super().__init__(message, *args)


def parse_rpc_error(error: dict, node_url: str = "") -> Exception:
    """Convert an RPC error dict into the appropriate Python exception."""
    code = error.get("code", 0)
    msg = error.get("message", "Unknown RPC error")

    if code == -32005 or "limit" in msg.lower():
        return LogLimitExceededError(msg)
    elif code == -32603 or code == -32700:
        return InvalidResponseError(msg, node_url=node_url)
    else:
        return Exception(f"RPC Error ({code}): {msg}")


# ============================================================
# JsonRpcClient -- lightweight client with attr-chain support
# ============================================================

import random


class _JsonRpcMethod:
    """Represents a callable RPC method like ``client.eth_get_block``.

    When called, it sends the actual JSON-RPC request.
    """

    def __init__(self, session: aiohttp.ClientSession, url: str,
                 method_path: str, timeout: int = 30):
        self._session = session
        self._url = url
        self._method_path = method_path
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def __call__(self, *args, **kwargs) -> Any:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": self._method_path,
            "params": list(args) if args else kwargs.get("params", []),
            "id": random.randint(1, 2**32),
        }
        try:
            async with self._session.post(
                self._url, json=payload, timeout=self._timeout
            ) as resp:
                if resp.status == 429:
                    raise RpcRateLimitError(
                        message=f"Rate limited by {self._url}",
                        node_url=self._url,
                    )
                if resp.status != 200:
                    raise InvalidResponseError(
                        message=f"HTTP {resp.status} from {self._url}",
                        node_url=self._url,
                    )
                data = await resp.json()
        except aiohttp.ServerTimeoutError:
            raise RpcTimeoutError(message=f"Timeout on {self._url}", node_url=self._url)
        except aiohttp.ClientError as e:
            raise InvalidResponseError(message=f"Connection error: {e}", node_url=self._url)

        if "error" in data:
            raise parse_rpc_error(data["error"], self._url)
        return data.get("result")


class JsonRpcClient:
    """Lightweight async JSON-RPC HTTP client.

    Supports attribute-chain navigation so it works seamlessly with
    apipool-ng's :class:`~apipool.manager.ChainProxy`.

    Example::

        client = JsonRpcClient(session, "https://rpc.example.com")
        await client.eth_blockNumber()
        await client.eth_getBlockByNumber("0x123", False)
        await client.eth_getLogs({"fromBlock": "0x1", "toBlock": "0x10"})
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        timeout: int = 30,
    ):
        self._session = session
        self._url = url
        self._timeout = timeout

    @property
    def url(self) -> str:
        return self._url

    def __getattr__(self, item: str) -> Any:
        """Return a callable RPC method proxy."""
        return _JsonRpcMethod(
            session=self._session,
            url=self._url,
            method_path=item,
            timeout=self._timeout,
        )


# ============================================================
# EthRpcApiKey -- one URL = one ApiKey
# ============================================================

class EthRpcApiKey(ApiKey):
    """Wraps a single EVM RPC endpoint URL as an apipool ApiKey.

    The primary key is the URL string itself.
    ``create_client()`` returns a :class:`JsonRpcClient`.
    """

    def __init__(self, url: str, *, priority: int = 100,
                 node_type: str = "public",
                 requests_per_second: float = 5.0):
        self.url = url
        self.priority = priority
        self.node_type = node_type
        self.requests_per_second = requests_per_second
        self._session: Optional[aiohttp.ClientSession] = None

    # --- ApiKey interface ---

    def get_primary_key(self) -> str:
        return self.url

    def create_client(self) -> JsonRpcClient:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return JsonRpcClient(self._session, self.url)

    def test_usability(self, client: Any):
        """Quick health check: call eth_blockNumber."""
        return client.eth_blockNumber()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# EvmRpcPool -- high-level pool manager (server-driven)
# ============================================================

class EvmRpcPool:
    """EVM RPC connection pool powered by **apipool-ng** AsyncDynamicKeyManager.

    Delegates ALL operational concerns to the apipool-server:

    - **Node selection** -- server's rotation strategy (random / round-robin /
      least-used).
    - **Key eviction** -- server's ``reach_limit_exception`` rules (default:
      any ``Exception`` triggers swap since v1.0.7).
    - **Concurrency / rate-limit** -- synced from server ``pool_config``.
    - **Health & banning** -- server tracks consecutive failures and bans.
    - **Auto-refresh** -- ``AsyncDynamicKeyManager`` periodically re-fetches the
      key list so added / removed endpoints are picked up without restart.

    Two construction modes:

    1. **apipool-server** (recommended) -- ``from_server()`` uses
       ``AsyncDynamicKeyManager`` for fully automatic lifecycle management::

           pool = await EvmRpcPool.from_server(
               service_url="http://apipool-server:8000",
               pool_identifier="my-eth-pool",
               username="alice", password="secret",
               chain_id=1, chain_name="ethereum",
           )

    2. **Local URLs** (backward-compat) -- falls back to a static key list
       with a plain ``ApiKeyManager`` (no server sync)::

           pool = EvmRpcPool(urls=["https://n1.com", ...], ...)
    """

    # Default refresh interval (seconds) for key-list sync from server.
    DEFAULT_REFRESH_INTERVAL: float = 60.0

    def __init__(
        self,
        urls: List[str],
        chain_id: int = 0,
        chain_name: str = "",
        *,
        timeout_seconds: int = 30,
    ):
        """Create pool from a static list of URLs.

        .. note::
           This constructor creates a **static** pool that does NOT sync with
           an apipool-server.  Use :meth:`from_server` for server-driven mode.

        Args:
            urls: List of RPC endpoint URLs.
            chain_id: EVM chain ID.
            chain_name: Human-readable name.
            timeout_seconds: Per-request HTTP timeout.
        """
        self.chain_id = chain_id
        self.chain_name = chain_name
        self._timeout = timeout_seconds
        self._server_mode = False

        # Build static ApiKey list
        apikey_list = [EthRpcApiKey(url) for url in urls]
        from apipool import ApiKeyManager
        self._manager: Union[ApiKeyManager, AsyncDynamicKeyManager] = ApiKeyManager(
            apikey_list=apikey_list,
        )

    # ------------------------------------------------------------------
    # Primary factory: apipool-server driven mode
    # ------------------------------------------------------------------

    @classmethod
    async def from_server(
        cls,
        service_url: str,
        pool_identifier: str,
        username: str,
        password: str,
        chain_id: int = 0,
        chain_name: str = "",
        *,
        timeout_seconds: int = 30,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ) -> "EvmRpcPool":
        """Create an EvmRpcPool backed by an apipool-server instance.

        Uses ``AsyncDynamicKeyManager`` which automatically:

        1. Authenticates via ``alogin``.
        2. Fetches raw key (URL) list via ``aget_keys``.
        3. Periodically refreshes the key list (add/remove detected).
        4. Syncs ``pool_config`` (concurrency, timeouts, retries, ban
           settings) from the server.

        Args:
            service_url: Base URL of the apipool-server.
            pool_identifier: Pool identifier on the server.
            username: Login username.
            password: Login password.
            chain_id: EVM chain ID.
            chain_name: Human-readable chain name.
            timeout_seconds: Per-request HTTP timeout.
            refresh_interval: Seconds between key-list refreshes.

        Returns:
            A fully initialized, started ``EvmRpcPool``.
        """
        # 1. Authenticate
        tokens = await alogin(service_url, username, password)
        auth_token = tokens["access_token"]

        # 2. Build key-fetcher closure (captures auth details)
        async def _key_fetcher():
            try:
                raw_keys = await aget_keys(service_url, pool_identifier, auth_token)
                logger.info(
                    "[DEBUG] _key_fetcher success: url=%s, pool=%s, keys_count=%d",
                    service_url, pool_identifier, len(raw_keys),
                )
                return raw_keys
            except Exception as exc:
                logger.error(
                    "[DEBUG] _key_fetcher FAILED: url=%s, pool=%s, error=%s: %s",
                    service_url, pool_identifier, type(exc).__name__, str(exc),
                    exc_info=True,
                )
                raise

        # 3. Build config-fetcher closure (tolerant of missing endpoint / 404)
        async def _config_fetcher():
            try:
                return await aget_config(service_url, pool_identifier, auth_token)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.warning(
                        "Config endpoint returned 404 for pool '%s' — "
                        "server may not have /{id}/config route or pool does not exist. "
                        "Using defaults.",
                        pool_identifier,
                    )
                    return PoolConfig()  # return empty/default config
                raise
            except Exception:
                logger.warning(
                    "Config sync failed for pool '%s', using defaults",
                    pool_identifier,
                    exc_info=True,
                )
                return PoolConfig()

        # 4. Build API-key factory
        def _api_key_factory(raw_key: str) -> EthRpcApiKey:
            return EthRpcApiKey(raw_key)

        # 5. Create the instance with empty initial key list (async init follows)
        pool = cls.__new__(cls)
        pool.chain_id = chain_id
        pool.chain_name = chain_name
        pool._timeout = timeout_seconds
        pool._server_mode = True
        pool._server_info: Dict[str, Any] = {
            "service_url": service_url,
            "pool_identifier": pool_identifier,
            "username": username,
        }

        # 6. Construct AsyncDynamicKeyManager
        pool._manager = AsyncDynamicKeyManager(
            key_fetcher=_key_fetcher,
            api_key_factory=_api_key_factory,
            refresh_interval=refresh_interval,
            config_fetcher=_config_fetcher,
        )

        # 7. Perform initial async init (fetch keys, connect clients)
        await pool._manager.ainit()

        logger.info(
            "EvmRpcPool created from server: %s (pool=%s, chain=%d)",
            service_url, pool_identifier, chain_id,
        )
        return pool

    # ------------------------------------------------------------------
    # Public API -- mirrors RPCNodePool interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background refresh (only meaningful in server mode)."""
        if isinstance(self._manager, AsyncDynamicKeyManager) and self._server_mode:
            await self._manager.astart()
            logger.info("EvmRpcPool background refresh started")

    async def stop(self) -> None:
        """Stop background refresh and close resources."""
        if isinstance(self._manager, AsyncDynamicKeyManager):
            try:
                await self._manager.ashutdown()
            except Exception:
                pass
        await self.close()

    async def get_block_number(self) -> int:
        """Fetch latest block number via adummyclient (auto load-balanced)."""
        try:
            result = await self._manager.adummyclient.eth_blockNumber()
            if result is None:
                raise InvalidResponseError("eth_blockNumber returned null")
            return int(result, 16)
        except PoolExhaustedError:
            raise AllNodesFailedError(
                f"All RPC nodes exhausted for {self.chain_name}"
            )
        except (RpcRateLimitError, RpcTimeoutError, InvalidResponseError):
            raise
        except Exception as e:
            raise InvalidResponseError(str(e))

    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
    ) -> list:
        """Call eth_getLogs via adummyclient (auto load-balanced).

        Returns the raw log list (list of dicts).
        """
        params: Dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics

        try:
            result = await self._manager.adummyclient.eth_getLogs(params)

            if result is None:
                return []

            # Check for truncation warning
            if len(result) >= 10000:
                raise LogLimitExceededError(
                    f"eth_getLogs returned {len(result)} results "
                    f"(may be truncated)"
                )
            return result
        except PoolExhaustedError:
            raise AllNodesFailedError(
                f"All RPC nodes exhausted for {self.chain_name}"
            )
        except (LogLimitExceededError, RpcRateLimitError, RpcTimeoutError):
            raise
        except Exception as e:
            err_str = str(e).lower()
            if "limit" in err_str or "query" in err_str:
                raise LogLimitExceededError(str(e))
            raise InvalidResponseError(str(e))

    async def raw_call(self, method: str, params: list = None) -> Any:
        """Send a raw JSON-RPC call through adummyclient."""
        try:
            caller = getattr(self._manager.adummyclient, method)
            return await caller(*(params or []))
        except PoolExhaustedError:
            raise AllNodesFailedError(
                f"All RPC nodes exhausted for {self.chain_name}"
            )

    async def health_check(self) -> Dict[str, Any]:
        """Check all active endpoints, return health status."""
        healthy_urls = []
        failed_urls = []
        active_url = ""

        for pk, apikey in self._manager.apikey_chain.items():
            try:
                num = await self._call_on_apikey(apikey, "eth_blockNumber")
                healthy_urls.append(pk)
                if not active_url:
                    active_url = pk
            except Exception:
                failed_urls.append(pk)

        archived_count = len(self._manager.archived_apikey_chain)

        return {
            "active": active_url,
            "available": healthy_urls,
            "failed": failed_urls,
            "archived": archived_count,
        }

    async def close(self) -> None:
        """Close all underlying HTTP sessions."""
        for apikey in list(self._manager.apikey_chain.values()):
            if hasattr(apikey, "close"):
                await apikey.close()
        for apikey in list(self._manager.archived_apikey_chain.values()):
            if hasattr(apikey, "close"):
                await apikey.close()

    # ------------------------------------------------------------------
    # Access to underlying manager (for advanced usage / diagnostics)
    # ------------------------------------------------------------------

    @property
    def manager(self) -> Union[Any, "AsyncDynamicKeyManager"]:
        """Access the underlying manager (ApiKeyManager or AsyncDynamicKeyManager)."""
        return self._manager

    @property
    def dummy_client(self):
        """Access the sync DummyClient for transparent proxy calls."""
        return self._manager.dummyclient

    @property
    def adummy_client(self):
        """Access the async DummyClient for transparent async calls."""
        return self._manager.adummyclient

    @property
    def stats(self):
        """Usage statistics collector."""
        return self._manager.stats

    @property
    def is_server_mode(self) -> bool:
        """True if this pool is backed by an apipool-server."""
        return self._server_mode

    async def check_usable(self) -> None:
        """Run usability check on all active endpoints."""
        self._manager.check_usable()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_on_apikey(self, apikey: EthRpcApiKey, method: str, *args) -> Any:
        """Execute a single RPC call on a specific ApiKey."""
        client = apikey._client
        method_obj = getattr(client, method)
        return await method_obj(*args)
