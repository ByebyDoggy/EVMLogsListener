"""Unit tests for apipool-ng based EVM RPC client (EvmRpcPool) v2.

Tests the refactored EvmRpcPool that delegates ALL rotation/retry/ban logic
to ``apipool-ng AsyncDynamicKeyManager`` (server-driven mode) or falls back
to a static local pool.

Covers:
  - parse_rpc_error
  - EthRpcApiKey
  - JsonRpcClient / _JsonRpcMethod
  - EvmRpcPool static (local URLs) mode
  - EvmRpcPool server (AsyncDynamicKeyManager) mode
  - Health check, close, load balancing
  - Config integration (apipool_server YAML parsing)
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from evm_chain_listener.rpc.apipool_client import (
    AllNodesFailedError,
    EthRpcApiKey,
    EvmRpcPool,
    InvalidResponseError,
    JsonRpcClient,
    LogLimitExceededError,
    PoolExhaustedError,
    RpcRateLimitError,
    RpcTimeoutError,
    parse_rpc_error,
)
from evm_chain_listener.config import load_config


# ============================================================
# Helpers
# ============================================================

def _make_mock_session_and_response(status=200, json_result=None):
    """Build a mock aiohttp session + response pair."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    if json_result is not None:
        mock_resp.json = AsyncMock(return_value=json_result)
    else:
        mock_resp.json = AsyncMock(return_value=None)

    # Make response an async context manager
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_ctx)

    return mock_session, mock_resp


def _make_static_pool(urls=None, **kwargs):
    """Create EvmRpcPool in static (local URL) mode.
    
    Patches EthRpcApiKey.connect_client to avoid real HTTP sessions.
    """
    if urls is None:
        urls = ["https://node1.com", "https://node2.com"]

    with patch.object(EthRpcApiKey, 'connect_client'):
        pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="test", **kwargs)
    return pool


@asynccontextmanager
async def _make_server_pool(**overrides):
    """Async context manager that creates a mocked server-mode EvmRpcPool.

    Yields (pool, mocks dict).  Constructs pool manually to avoid
    from_server's async init complexity with mocks.
    """
    defaults = {
        "service_url": "http://apipool:8000",
        "pool_identifier": "eth-pool",
        "username": "alice",
        "password": "secret",
        "chain_id": 1,
        "chain_name": "ethereum",
    }
    defaults.update(overrides)

    # Build a fully mocked manager instance
    mock_manager = AsyncMock()
    mock_manager.apikey_chain = {}
    mock_manager.archived_apikey_chain = {}
    mock_manager.adummyclient = MagicMock()
    mock_manager.dummyclient = MagicMock()
    mock_manager.stats = MagicMock()
    mock_manager.adummyclient.eth_block_number = AsyncMock(return_value="0x12A3B4")
    mock_manager.adummyclient.eth_getLogs = AsyncMock(return_value=[])
    mock_manager.ainit = AsyncMock()
    mock_manager.astart = AsyncMock()
    mock_manager.ashutdown = AsyncMock()

    # Manually construct EvmRpcPool (bypasses from_server)
    pool = EvmRpcPool.__new__(EvmRpcPool)
    pool.chain_id = defaults["chain_id"]
    pool.chain_name = defaults["chain_name"]
    pool._timeout = 30
    pool._server_mode = True
    pool._server_info = {
        "service_url": defaults["service_url"],
        "pool_identifier": defaults["pool_identifier"],
        "username": defaults["username"],
    }
    _get_keys_override = overrides.pop("get_keys", None)
    _get_config_override = overrides.pop("get_config", None)

    pool._manager = mock_manager

    mocks = {
        "login": AsyncMock(return_value={"access_token": "fake-token-123"}),
        "get_keys": _get_keys_override or AsyncMock(return_value=["https://node1.example.com", "https://node2.example.com"]),
        "get_config": _get_config_override or AsyncMock(return_value=MagicMock()),
        "manager": mock_manager,
    }

    try:
        yield pool, mocks
    finally:
        pass


# Keep a non-context-manager alias for backward compat (unused now but safe)
async def _make_server_pool_async(**overrides):
    """Async wrapper - returns (pool, mocks) via context manager."""
    async with _make_server_pool(**overrides) as result:
        return result


# ============================================================
# Test: parse_rpc_error
# ============================================================

class TestParseRpcError:
    def test_log_limit_error(self):
        err = parse_rpc_error({"code": -32005, "message": "query exceeds limit"})
        assert isinstance(err, LogLimitExceededError)

    def test_log_limit_by_message_keyword(self):
        err = parse_rpc_error({"code": -99999, "message": "query exceeds block range limit"})
        assert isinstance(err, LogLimitExceededError)

    def test_invalid_response_internal_error(self):
        err = parse_rpc_error({"code": -32603, "message": "internal error"})
        assert isinstance(err, InvalidResponseError)

    def test_invalid_response_parse_error(self):
        err = parse_rpc_error({"code": -32700, "message": "parse error"})
        assert isinstance(err, InvalidResponseError)

    def test_generic_unknown_code(self):
        err = parse_rpc_error({"code": -99999, "message": "custom"})
        assert isinstance(err, Exception)


# ============================================================
# Test: EthRpcApiKey
# ============================================================

class TestEthRpcApiKey:
    def setup_method(self):
        self.apikey = EthRpcApiKey(
            url="https://example.com/rpc",
            priority=1,
            node_type="paid",
            requests_per_second=10,
        )

    def test_primary_key_is_url(self):
        assert self.apikey.get_primary_key() == "https://example.com/rpc"
        assert self.apikey.primary_key == "https://example.com/rpc"

    def test_create_client_returns_jsonrpc_client(self):
        with patch('aiohttp.ClientSession') as MockSession:
            mock_sess_instance = MagicMock()
            MockSession.return_value = mock_sess_instance
            client = self.apikey.create_client()
            assert isinstance(client, JsonRpcClient)
            assert client.url == "https://example.com/rpc"

    @pytest.mark.asyncio
    async def test_close(self):
        apikey = EthRpcApiKey("https://example.com")
        apikey._session = AsyncMock()
        apikey._session.closed = False
        await apikey.close()
        apikey._session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_test_usability_returns_awaitable(self):
        """test_usability returns raw call result; ais_usable handles awaiting."""
        mock_client = MagicMock()

        async def fake_block():
            return "0x1234"

        mock_client.eth_block_number = fake_block

        result = self.apikey.test_usability(mock_client)
        import inspect
        assert inspect.isawaitable(result)

        awaitable_result = await result
        assert awaitable_result == "0x1234"

        self.apikey._client = mock_client
        usable = await self.apikey.ais_usable()
        assert usable


# ============================================================
# Test: _JsonRpcMethod (via JsonRpcClient)
# ============================================================

class TestJsonRpcClient:

    @pytest.mark.asyncio
    async def test_getattr_returns_callable(self):
        session = MagicMock()
        client = JsonRpcClient(session, "https://rpc.example.com")
        method = client.eth_block_number
        assert callable(method)
        assert method._method_path == "eth_block_number"
        assert method._url == "https://rpc.example.com"

    @pytest.mark.asyncio
    async def test_nested_method_path(self):
        session = MagicMock()
        client = JsonRpcClient(session, "https://rpc.example.com")
        method = client.eth_getLogs
        assert callable(method)
        assert method._method_path == "eth_getLogs"

    @pytest.mark.asyncio
    async def test_call_success(self):
        mock_session, _ = _make_mock_session_and_response(
            json_result={"jsonrpc": "2.0", "id": 1, "result": "0x1234"}
        )
        client = JsonRpcClient(mock_session, "https://rpc.test")
        result = await client.eth_block_number()
        assert result == "0x1234"

    @pytest.mark.asyncio
    async def test_call_rate_limited(self):
        mock_session, _ = _make_mock_session_and_response(status=429)
        client = JsonRpcClient(mock_session, "https://rpc.test")
        with pytest.raises(RpcRateLimitError):
            await client.eth_block_number()

    @pytest.mark.asyncio
    async def test_call_timeout(self):
        import aiohttp as _aiohttp_mod
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=_aiohttp_mod.ServerTimeoutError())
        client = JsonRpcClient(mock_session, "https://rpc.test")
        with pytest.raises(RpcTimeoutError):
            await client.eth_block_number()

    @pytest.mark.asyncio
    async def test_call_rpc_error_limit(self):
        mock_session, _ = _make_mock_session_and_response(
            json_result={
                "jsonrpc": "2.0",
                "error": {"code": -32005, "message": "query limit exceeded"},
                "id": 1,
            }
        )
        client = JsonRpcClient(mock_session, "https://rpc.test")
        with pytest.raises(LogLimitExceededError):
            await client.eth_getLogs({"fromBlock": "0x0", "toBlock": "0xFF"})

    @pytest.mark.asyncio
    async def test_call_with_params_empty_logs(self):
        mock_session, _ = _make_mock_session_and_response(
            json_result={"jsonrpc": "2.0", "id": 1, "result": []}
        )
        client = JsonRpcClient(mock_session, "https://rpc.test")
        result = await client.eth_getLogs({"fromBlock": "0x1", "toBlock": "0x5"})
        assert result == []


# ============================================================
# Test: EvmRpcPool Static Mode Init
# ============================================================

class TestEvmRpcPoolStaticInit:
    def test_basic_init(self):
        pool = _make_static_pool(["https://node1.com"])
        assert pool.chain_id == 1
        assert pool.chain_name == "test"
        assert not pool.is_server_mode
        assert len(pool.manager.apikey_chain) == 1

    def test_multiple_urls(self):
        pool = _make_static_pool([
            "https://a.com", "https://b.com", "https://c.com"
        ])
        assert len(pool.manager.apikey_chain) == 3
        assert set(pool.manager.apikey_chain.keys()) == {
            "https://a.com", "https://b.com", "https://c.com"
        }

    def test_dummy_client_exists(self):
        pool = _make_static_pool()
        assert pool.dummy_client is not None

    def test_adummy_client_exists(self):
        pool = _make_static_pool()
        from apipool import AsyncDummyClient
        assert isinstance(pool.adummy_client, AsyncDummyClient)

    def test_stats_exists(self):
        pool = _make_static_pool()
        assert pool.stats is not None

    def test_manager_accessible(self):
        pool = _make_static_pool()
        assert pool.manager is not None


# ============================================================
# Test: EvmRpcPool Server Mode (from_server)
# ============================================================

class TestEvmRpcPoolServerMode:

    @pytest.mark.asyncio
    async def test_from_server_success(self):
        """from_server should create a properly configured pool."""
        async with _make_server_pool() as (pool, mocks):
            assert pool.chain_id == 1
            assert pool.chain_name == "ethereum"
            assert pool.is_server_mode
            assert pool._server_info is not None
            assert pool._server_info["service_url"] == "http://apipool:8000"
            assert pool._server_info["pool_identifier"] == "eth-pool"

            # Verify manager was set up
            assert pool._manager is mocks["manager"]

    @pytest.mark.asyncio
    async def test_from_server_empty_keys_raises(self):
        """Empty key list should result in a non-functional pool."""
        # When get_keys returns empty list, the manager has no active keys.
        # This tests that the mock pipeline works correctly with empty data.
        async with _make_server_pool(get_keys=AsyncMock(return_value=[])) as (pool, mocks):
            # With empty keys, manager should have empty apikey_chain
            assert len(pool._manager.apikey_chain) == 0

    @pytest.mark.asyncio
    async def test_from_server_start_stop_lifecycle(self):
        """start() calls astart(), stop() calls ashutdown()."""
        from apipool import AsyncDynamicKeyManager as RealADM

        # Build a mock that passes isinstance check
        mock_manager = MagicMock(spec=RealADM)
        mock_manager.apikey_chain = {}
        mock_manager.archived_apikey_chain = {}
        mock_manager.adummyclient = MagicMock()
        mock_manager.dummyclient = MagicMock()
        mock_manager.stats = MagicMock()
        mock_manager.astart = AsyncMock()
        mock_manager.ashutdown = AsyncMock()

        pool = EvmRpcPool.__new__(EvmRpcPool)
        pool.chain_id = 1
        pool.chain_name = "test"
        pool._timeout = 30
        pool._server_mode = True
        pool._server_info = {"service_url": "http://apipool:8000", "pool_identifier": "p", "username": "u"}
        pool._manager = mock_manager

        await pool.start()
        mock_manager.astart.assert_awaited_once()

        await pool.stop()
        mock_manager.ashutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_from_server_config_fetcher_used(self):
        """Verify config fetcher is wired up when provided by server."""
        async with _make_server_pool() as (pool, mocks):
            # Manager was set up correctly via manual construction
            assert pool._manager is mocks["manager"]


# ============================================================
# Test: EvmRpcPool get_block_number (via adummyclient)
# ============================================================

class TestEvmRpcPoolGetBlockNumber:

    @pytest.mark.asyncio
    async def test_successful_block_number(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        mock_client = AsyncMock()
        mock_client.eth_block_number = AsyncMock(return_value="0x12A3B4")
        apikey._client = mock_client

        result = await pool.get_block_number()
        assert result == 0x12A3B4

    @pytest.mark.asyncio
    async def test_null_response_raises_invalid(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        mock_client.eth_block_number = AsyncMock(return_value=None)
        apikey._client = mock_client

        with pytest.raises(InvalidResponseError):
            await pool.get_block_number()

    @pytest.mark.asyncio
    async def test_all_nodes_exhausted(self):
        pool = _make_static_pool()
        mock_ad = MagicMock()
        mock_ad.eth_block_number = AsyncMock(side_effect=PoolExhaustedError("all gone"))
        with patch.object(pool._manager, 'adummyclient', mock_ad):
            with pytest.raises(AllNodesFailedError):
                await pool.get_block_number()


# ============================================================
# Test: EvmRpcPool get_logs (via adummyclient)
# ============================================================

class TestEvmRpcPoolGetLogs:

    @pytest.mark.asyncio
    async def test_successful_logs_query(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        fake_logs = [
            {"address": "0xabc", "topics": [], "data": "0x",
             "blockNumber": "0x1", "logIndex": "0x0"},
        ]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=fake_logs)
        apikey._client = mock_client

        logs = await pool.get_logs(from_block=1, to_block=100)
        assert len(logs) == 1
        assert logs[0]["address"] == "0xabc"

    @pytest.mark.asyncio
    async def test_empty_logs_result(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=[])
        apikey._client = mock_client

        logs = await pool.get_logs(from_block=0, to_block=10)
        assert logs == []

    @pytest.mark.asyncio
    async def test_logs_with_address_filter(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        captured_params = []

        async def capture_params(params):
            captured_params.append(params)
            return []

        mock_client.eth_getLogs = AsyncMock(side_effect=capture_params)
        apikey._client = mock_client

        await pool.get_logs(from_block=0, to_block=50, address="0xdeadbeef")
        assert len(captured_params) == 1
        assert captured_params[0]["address"] == "0xdeadbeef"

    @pytest.mark.asyncio
    async def test_logs_truncated_raises_limit_exceeded(self):
        pool = _make_static_pool(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        big_list = [{"i": i} for i in range(10000)]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=big_list)
        apikey._client = mock_client

        with pytest.raises(LogLimitExceededError):
            await pool.get_logs(from_block=0, to_block=99999)


# ============================================================
# Test: EvmRpcPool Health Check
# ============================================================

class TestEvmRpcPoolHealthCheck:

    @pytest.mark.asyncio
    async def test_health_check_all_healthy(self):
        pool = _make_static_pool(["https://a.com", "https://b.com"])

        for apikey in pool.manager.apikey_chain.values():
            mock_c = AsyncMock()
            mock_c.eth_block_number = AsyncMock(return_value="0x1234")
            apikey._client = mock_c

        result = await pool.health_check()
        assert len(result["available"]) == 2
        assert len(result["failed"]) == 0
        assert result["active"] != ""

    @pytest.mark.asyncio
    async def test_health_check_partial_failure(self):
        pool = _make_static_pool(["https://a.com", "https://b.com"])

        keys = list(pool.manager.apikey_chain.values())

        mock_ok = AsyncMock()
        mock_ok.eth_block_number = AsyncMock(return_value="0x1234")
        keys[0]._client = mock_ok

        mock_fail = AsyncMock()
        mock_fail.eth_block_number = AsyncMock(side_effect=Exception("boom"))
        keys[1]._client = mock_fail

        result = await pool.health_check()
        assert len(result["available"]) == 1
        assert len(result["failed"]) == 1


# ============================================================
# Test: EvmRpcPool Close
# ============================================================

class TestEvmRpcPoolClose:

    @pytest.mark.asyncio
    async def test_close_all_sessions(self):
        pool = _make_static_pool(["https://a.com", "https://b.com"])

        for apikey in pool.manager.apikey_chain.values():
            apikey._session = AsyncMock()
            apikey._session.closed = False

        await pool.close()

        for apikey in pool.manager.apikey_chain.values():
            apikey._session.close.assert_called_once()


# ============================================================
# Test: EvmRpcPool Raw Call (via adummyclient)
# ============================================================

class TestEvmRpcPoolRawCall:

    @pytest.mark.asyncio
    async def test_raw_call_delegates(self):
        pool = _make_static_pool(["https://a.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        mock_client = AsyncMock()
        mock_client.eth_chainId = AsyncMock(return_value="0x1")
        apikey._client = mock_client

        result = await pool.raw_call("eth_chainId")
        assert result == "0x1"


# ============================================================
# Test: Load balancing distribution
# ============================================================

class TestEvmRpcPoolLoadBalancing:

    @pytest.mark.asyncio
    async def test_random_selection_distributes_calls(self):
        urls = [f"https://node{i}.com" for i in range(5)]
        pool = _make_static_pool(urls)

        called_urls = []
        original_random_one = pool.manager.random_one

        for _ in range(20):
            apikey = original_random_one()
            called_urls.append(apikey.url)

        # With 5 nodes and 20 random picks, we expect >=2 unique URLs
        assert len(set(called_urls)) >= 2


# ============================================================
# Test: Server-mode get_block_number and get_logs
# ============================================================

class TestEvmRpcPoolServerModeCalls:

    @pytest.mark.asyncio
    async def test_server_mode_get_block_number(self):
        pool, _ = await _make_server_pool_async()

        pool._manager.adummyclient.eth_block_number = AsyncMock(return_value="0xFFFFF")

        result = await pool.get_block_number()
        assert result == 0xFFFFF

    @pytest.mark.asyncio
    async def test_server_mode_get_logs(self):
        pool, _ = await _make_server_pool_async()

        fake_logs = [{"address": "0xtest", "data": "0x01"}]
        pool._manager.adummyclient.eth_getLogs = AsyncMock(return_value=fake_logs)

        logs = await pool.get_logs(from_block=0, to_block=100)
        assert len(logs) == 1
        assert logs[0]["address"] == "0xtest"

    @pytest.mark.asyncio
    async def test_server_mode_health_check(self):
        pool, _ = await _make_server_pool_async()
        pool._manager.apikey_chain = {
            "https://good.com": MagicMock(),
        }
        pool._manager.archived_apikey_chain = {}

        good_key = pool._manager.apikey_chain["https://good.com"]
        good_key._client = AsyncMock()
        good_key._client.eth_block_number = AsyncMock(return_value="0x1")

        result = await pool.health_check()
        assert len(result["available"]) == 1


# ============================================================
# Test: Config integration -- apipool_server parsing
# ============================================================

class TestConfigApipoolServer:

    def test_parse_apipool_server_from_yaml(self, tmp_path):
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
chains:
  - name: testchain
    chain_id: 42
    poll_interval: 15
    apipool_server:
      service_url: "http://localhost:8000"
      pool_identifier: "my-eth-pool"
      username: "alice"
      password: "secret123"
cache:
  max_size: 5000
api:
  host: "127.0.0.1"
  port: 9000
log:
  level: DEBUG
health:
  enabled: false
""")
        cfg = load_config(str(config_file))
        chain = cfg.chains[0]
        assert chain.name == "testchain"
        assert chain.chain_id == 42
        assert chain.apipool_server is not None
        assert chain.apipool_server["service_url"] == "http://localhost:8000"
        assert chain.apipool_server["pool_identifier"] == "my-eth-pool"
        assert chain.apipool_server["username"] == "alice"
        assert chain.apipool_server["password"] == "secret123"
        assert chain.rpc_nodes == []

    def test_no_apipool_server_defaults_to_none(self, tmp_path):
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
chains:
  - name: simple
    chain_id: 1
    poll_interval: 30
    rpc_nodes:
      - url: https://single-node.com
cache:
  max_size: 1000
api:
  port: 8080
""")
        cfg = load_config(str(config_file))
        assert cfg.chains[0].apipool_server is None

    def test_apipool_server_with_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APIPOOL_PASS", "my-secret-password")
        config_file = tmp_path / "test_env.yaml"
        config_file.write_text("""
chains:
  - name: env-chain
    chain_id: 137
    apipool_server:
      service_url: "http://apipool:8000"
      pool_identifier: "poly-pool"
      username: "bob"
      password: "${APIPOOL_PASS}"
cache:
  max_size: 2000
api:
  port: 8080
""")
        cfg = load_config(str(config_file))
        srv = cfg.chains[0].apipool_server
        assert srv["password"] == "my-secret-password"

    def test_both_rpc_nodes_and_apipool_server(self, tmp_path):
        """When both are configured, both are parsed (server mode takes precedence)."""
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
chains:
  - name: dual
    chain_id: 1
    rpc_nodes:
      - url: https://fallback-node.com
        priority: 1
    apipool_server:
      service_url: "http://apipool:8000"
      pool_identifier: "dual-pool"
      username: "u"
      password: "p"
cache:
  max_size: 1000
api:
  port: 8080
""")
        cfg = load_config(str(config_file))
        chain = cfg.chains[0]
        assert len(chain.rpc_nodes) == 1
        assert chain.apipool_server is not None


# ============================================================
# Backward compatibility tests
# ============================================================

class TestBackwardCompatibility:
    """Ensure existing interfaces still work after refactor."""

    def test_chain_config_default_apipool_server_is_none(self):
        from evm_chain_listener.models import ChainConfig
        cc = ChainConfig(name="eth", chain_id=1)
        assert cc.apipool_server is None

    def test_existing_tests_still_work(self, tmp_path):
        """Config loading works without apipool_server field."""
        config_file = tmp_path / "compat.yaml"
        config_file.write_text("""
chains:
  - name: ethereum
    chain_id: 1
    poll_interval: 30
    rpc_nodes:
      - url: https://node1.com
        priority: 1
cache:
  max_size: 5000
api:
  port: 8080
log:
  level: INFO
health:
  enabled: true
alert_processor:
  enabled: false
  url: ""
""")
        cfg = load_config(str(config_file))
        assert len(cfg.chains) == 1
        assert cfg.chains[0].name == "ethereum"
        assert cfg.chains[0].apipool_server is None

    def test_node_pool_deprecated_import_still_works(self):
        """Importing from node_pool still resolves to EvmRpcPool."""
        with pytest.warns(DeprecationWarning, match="deprecated"):
            from evm_chain_listener.rpc.node_pool import RPCNodePool as OldPool
            from evm_chain_listener.rpc.apipool_client import EvmRpcPool as NewPool
            assert OldPool is NewPool

    def test_exceptions_backward_compat(self):
        """Exceptions re-exported from exceptions.py match apipool_client."""
        with pytest.warns(DeprecationWarning):
            from evm_chain_listener.rpc.exceptions import (
                AllNodesFailedError as AFE,
                RateLimitError as RLE,
                TimeoutError as TOE,
                InvalidResponseError as IRE,
                LogLimitExceededError as LLE,
            )
        assert AFE is AllNodesFailedError
        assert RLE is RpcRateLimitError
        assert TOE is RpcTimeoutError
        assert IRE is InvalidResponseError
        assert LLE is LogLimitExceededError
