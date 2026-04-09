"""EVM RPC Client backed by apipool-ng ApiKeyManager.

Provides:
  - ``EthRpcApiKey``: maps one RPC endpoint URL → one ApiKey
  - ``EvmRpcPool``: high-level pool manager wrapping ApiKeyManager,
    with the same interface as the original ``RPCNodePool``
  - ``JsonRpcClient``: lightweight JSON-RPC client supporting
    attribute-chain navigation for use with ChainProxy.

Usage::

    from evm_chain_listener.rpc.apipool_client import EvmRpcPool, EthRpcApiKey

    urls = ["https://node1.example.com", "https://node2.example.com"]
    pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="ethereum")

    # Transparently load-balanced across all URLs
    block_num = await pool.get_block_number()
    logs = await pool.get_logs(from_block=0, to_block=100)
"""

import json
import random
from typing import Any, Dict, List, Optional

import aiohttp

from apipool import ApiKey, ApiKeyManager, PoolExhaustedError


# ============================================================
# Custom exceptions (mirrors original node_pool exceptions)
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
    pass


def parse_rpc_error(error: dict, node_url: str = "") -> Exception:
    """Convert an RPC error dict into the appropriate Python exception."""
    code = error.get("code", 0)
    msg = error.get("message", "Unknown RPC error")

    if code == -32005 or "limit" in msg.lower():
        return LogLimitExceededError(msg)
    elif code == -32603 or code == -32700:
        return InvalidResponseError(msg, node_url=node_url)
    else:
        # Return generic Exception (matches original behaviour)
        return Exception(f"RPC Error ({code}): {msg}")


# ============================================================
# JsonRpcClient — lightweight client with attr-chain support
# ============================================================

class _JsonRpcMethod:
    """Represents a callable RPC method like ``client.eth.get_block``.

    When called, it sends the actual JSON-RPC request.
    """

    def __init__(self, session: aiohttp.ClientSession, url: str,
                 method_path: str, timeout: int = 30):
        self._session = session
        self._url = url
        self._method_path = method_path  # e.g. "eth_getBlock"
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def __call__(self, *args, **kwargs) -> Any:
        payload = {
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

    Supports attribute-chain navigation so it works seamlessly
    with apipool-ng's :class:`~apipool.manager.ChainProxy`.

    Example::

        client = JsonRpcClient(session, "https://rpc.example.com")
        # These work via __getattr__ returning intermediate proxies:
        await client.eth_block_number()
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
        """Return a callable RPC method proxy.

        This enables both styles:

        - ``await client.eth_block_number()``   — flat method name
        - ``await client.eth.get_block('latest')`` — dotted chain
          (handled by ChainProxy wrapping us).
        """
        # Convert camelCase / snake_case RPC names to the exact string.
        # ChainProxy will handle multi-level chains like .eth.get_block
        # by calling __getattr__ multiple times; here we just return
        # a leaf callable.
        return _JsonRpcMethod(
            session=self._session,
            url=self._url,
            method_path=item,
            timeout=self._timeout,
        )


# ============================================================
# EthRpcApiKey — one URL = one ApiKey
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

    def test_usability(self, client: Any) -> bool:
        """Quick health check: call eth_blockNumber."""
        try:
            result = client.eth_block_number()
            # client methods are async-compatible but test_usability may be sync
            import asyncio
            if asyncio.iscoroutine(result):
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(result)
                finally:
                    loop.close()
            return result is not None
        except Exception:
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# EvmRpcPool — high-level pool manager
# ============================================================

class EvmRpcPool:
    """EVM RPC connection pool powered by **apipool-ng**.

    Features inherited from apipool-ng:
      - Random load balancing across all endpoints
      - Automatic key eviction on ``reach_limit_exc`` (e.g. 429)
      - Built-in usage statistics (SQLite in-memory)
      - Health check via ``check_usable()``

    Additional features over raw ApiKeyManager:
      - Async-native API matching original ``RPCNodePool`` interface
      - Per-node rate limiting awareness
      - Graceful degradation when pool is exhausted

    Example::

        pool = EvmRpcPool(
            urls=["https://node1.com", "https://node2.com"],
            chain_id=1,
            chain_name="ethereum",
        )

        block = await pool.get_block_number()
        logs  = await pool.get_logs(0, 100)
        await pool.close()
    """

    def __init__(
        self,
        urls: List[str],
        chain_id: int = 0,
        chain_name: str = "",
        *,
        reach_limit_exc: type = RpcRateLimitError,
        timeout_seconds: int = 30,
    ):
        self.chain_id = chain_id
        self.chain_name = chain_name
        self._timeout = timeout_seconds

        # Build ApiKey list
        apikey_list = [EthRpcApiKey(url) for url in urls]
        self._manager = ApiKeyManager(
            apikey_list=apikey_list,
            reach_limit_exc=reach_limit_exc,
        )

    # ------------------------------------------------------------------
    # Public API — mirrors RPCNodePool interface
    # ------------------------------------------------------------------

    async def get_block_number(self) -> int:
        """Fetch latest block number from a random healthy endpoint."""
        try:
            apikey = self._manager.random_one()
            client = apikey._client
            result = await client.eth_block_number()
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
        """Call eth_getLogs on a random endpoint.

        Returns the raw log list (list of dicts). Callers can convert
        to :class:`~evm_chain_listener.models.Log` objects as needed.
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
            apikey = self._manager.random_one()
            client = apikey._client
            result = await client.eth_getLogs(params)

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
        """Send a raw JSON-RPC call through the pool."""
        try:
            apikey = self._manager.random_one()
            client = apikey._client
            method_obj = getattr(client, method)
            return await method_obj(*(params or []))
        except PoolExhaustedError:
            raise AllNodesFailedError(
                f"All RPC nodes exhausted for {self.chain_name}"
            )

    async def health_check(self) -> Dict[str, Any]:
        """Check all endpoints, return health status."""
        healthy_urls = []
        failed_urls = []
        active_url = ""

        for pk, apikey in self._manager.apikey_chain.items():
            try:
                num = await self._call_on_apikey(apikey, "eth_block_number")
                healthy_urls.append(pk)
                if not active_url:
                    active_url = pk
            except Exception:
                failed_urls.append(pk)

        # Also check archived keys
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
    # Access to underlying manager (for advanced usage)
    # ------------------------------------------------------------------

    @property
    def manager(self) -> ApiKeyManager:
        """Access the underlying :class:`~apipool.manager.ApiKeyManager`."""
        return self._manager

    @property
    def dummy_client(self):
        """Access the DummyClient for transparent proxy calls.

        Example::

            pool.dummy_client.eth.get_block_by_number('latest', False)
        """
        return self._manager.dummyclient

    @property
    def stats(self):
        """Usage statistics collector."""
        return self._manager.stats

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
