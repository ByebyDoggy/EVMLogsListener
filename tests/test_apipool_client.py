"""Unit tests for apipool-ng based EVM RPC client (EvmRpcPool)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_pool_with_mocked_init(urls=None, **kwargs):
    """Create EvmRpcPool but prevent connect_client from creating real sessions."""
    if urls is None:
        urls = ["https://node1.com", "https://node2.com"]

    # Patch EthRpcApiKey.connect_client so no real session is created
    with patch.object(EthRpcApiKey, 'connect_client'):
        pool = EvmRpcPool(urls=urls, chain_id=1, chain_name="test", **kwargs)
    return pool


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
# Test: EvmRpcPool Init
# ============================================================

class TestEvmRpcPoolInit:
    def test_basic_init(self):
        pool = _make_pool_with_mocked_init(["https://node1.com"])
        assert pool.chain_id == 1
        assert pool.chain_name == "test"
        assert len(pool.manager.apikey_chain) == 1

    def test_multiple_urls(self):
        pool = _make_pool_with_mocked_init([
            "https://a.com", "https://b.com", "https://c.com"
        ])
        assert len(pool.manager.apikey_chain) == 3
        assert set(pool.manager.apikey_chain.keys()) == {
            "https://a.com", "https://b.com", "https://c.com"
        }

    def test_dummy_client_exists(self):
        pool = _make_pool_with_mocked_init()
        assert pool.dummy_client is not None

    def test_stats_exists(self):
        pool = _make_pool_with_mocked_init()
        assert pool.stats is not None

    def test_manager_accessible(self):
        pool = _make_pool_with_mocked_init()
        assert pool.manager is not None


# ============================================================
# Test: EvmRpcPool get_block_number
# ============================================================

class TestEvmRpcPoolGetBlockNumber:

    @pytest.mark.asyncio
    async def test_successful_block_number(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        mock_client = AsyncMock()
        mock_client.eth_block_number = AsyncMock(return_value="0x12A3B4")

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                result = await pool.get_block_number()

        assert result == 0x12A3B4

    @pytest.mark.asyncio
    async def test_null_response_raises_invalid(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        mock_client.eth_block_number = AsyncMock(return_value=None)

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                with pytest.raises(InvalidResponseError):
                    await pool.get_block_number()

    @pytest.mark.asyncio
    async def test_all_nodes_exhausted(self):
        pool = _make_pool_with_mocked_init()

        with patch.object(pool.manager, 'random_one',
                          side_effect=PoolExhaustedError("all gone")):
            with pytest.raises(AllNodesFailedError):
                await pool.get_block_number()


# ============================================================
# Test: EvmRpcPool get_logs
# ============================================================

class TestEvmRpcPoolGetLogs:

    @pytest.mark.asyncio
    async def test_successful_logs_query(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        fake_logs = [
            {"address": "0xabc", "topics": [], "data": "0x",
             "blockNumber": "0x1", "logIndex": "0x0"},
        ]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=fake_logs)

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                logs = await pool.get_logs(from_block=1, to_block=100)

        assert len(logs) == 1
        assert logs[0]["address"] == "0xabc"

    @pytest.mark.asyncio
    async def test_empty_logs_result(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=[])

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                logs = await pool.get_logs(from_block=0, to_block=10)

        assert logs == []

    @pytest.mark.asyncio
    async def test_logs_with_address_filter(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]
        mock_client = AsyncMock()
        captured_params = []

        async def capture_params(params):
            captured_params.append(params)
            return []

        mock_client.eth_getLogs = AsyncMock(side_effect=capture_params)

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                await pool.get_logs(
                    from_block=0, to_block=50,
                    address="0xdeadbeef",
                )

        assert len(captured_params) == 1
        assert captured_params[0]["address"] == "0xdeadbeef"

    @pytest.mark.asyncio
    async def test_logs_truncated_raises_limit_exceeded(self):
        pool = _make_pool_with_mocked_init(["https://n1.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        big_list = [{"i": i} for i in range(10000)]
        mock_client = AsyncMock()
        mock_client.eth_getLogs = AsyncMock(return_value=big_list)

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                with pytest.raises(LogLimitExceededError):
                    await pool.get_logs(from_block=0, to_block=99999)


# ============================================================
# Test: EvmRpcPool Health Check
# ============================================================

class TestEvmRpcPoolHealthCheck:

    @pytest.mark.asyncio
    async def test_health_check_all_healthy(self):
        pool = _make_pool_with_mocked_init(["https://a.com", "https://b.com"])

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
        pool = _make_pool_with_mocked_init(["https://a.com", "https://b.com"])

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
        pool = _make_pool_with_mocked_init(["https://a.com", "https://b.com"])

        for apikey in pool.manager.apikey_chain.values():
            apikey._session = AsyncMock()
            apikey._session.closed = False

        await pool.close()

        for apikey in pool.manager.apikey_chain.values():
            apikey._session.close.assert_called_once()


# ============================================================
# Test: EvmRpcPool Raw Call
# ============================================================

class TestEvmRpcPoolRawCall:

    @pytest.mark.asyncio
    async def test_raw_call_delegates(self):
        pool = _make_pool_with_mocked_init(["https://a.com"])
        apikey = list(pool.manager.apikey_chain.values())[0]

        mock_client = AsyncMock()
        mock_client.eth_chainId = AsyncMock(return_value="0x1")

        with patch.object(pool.manager, 'random_one', return_value=apikey):
            with patch.object(apikey, '_client', mock_client):
                result = await pool.raw_call("eth_chainId")

        assert result == "0x1"


# ============================================================
# Test: Load balancing distribution
# ============================================================

class TestEvmRpcPoolLoadBalancing:

    @pytest.mark.asyncio
    async def test_random_selection_distributes_calls(self):
        urls = [f"https://node{i}.com" for i in range(5)]
        pool = _make_pool_with_mocked_init(urls)

        called_urls = []
        original_random_one = pool.manager.random_one

        for _ in range(20):
            apikey = original_random_one()
            called_urls.append(apikey.url)

        # With 5 nodes and 20 random picks, we expect >=2 unique URLs
        assert len(set(called_urls)) >= 2


# ============================================================
# Test: Config integration — apipool_urls parsing
# ============================================================

class TestConfigApipoolUrls:

    def test_parse_apipool_urls_from_yaml(self, tmp_path):
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
chains:
  - name: testchain
    chain_id: 42
    poll_interval: 15
    rpc_nodes:
      - url: https://legacy-node.com
        priority: 1
    apipool_urls:
      - https://pool-a.com
      - https://pool-b.com
      - https://pool-c.com
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
        assert chain.apipool_urls == [
            "https://pool-a.com",
            "https://pool-b.com",
            "https://pool-c.com",
        ]
        assert len(chain.rpc_nodes) == 1

    def test_no_apipool_urls_defaults_to_none(self, tmp_path):
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
        assert cfg.chains[0].apipool_urls is None

    def test_apipool_urls_with_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RPC_URL_1", "https://env-node-1.com")
        monkeypatch.setenv("RPC_URL_2", "https://env-node-2.com")
        config_file = tmp_path / "test_env.yaml"
        config_file.write_text("""
chains:
  - name: env-chain
    chain_id: 137
    apipool_urls:
      - "${RPC_URL_1}"
      - "${RPC_URL_2}"
cache:
  max_size: 2000
api:
  port: 8080
""")
        cfg = load_config(str(config_file))
        assert cfg.chains[0].apipool_urls == [
            "https://env-node-1.com",
            "https://env-node-2.com",
        ]


# ============================================================
# Run existing tests to ensure backward compatibility
# ============================================================

class TestBackwardCompatibility:
    """Ensure existing tests still pass after model changes."""

    def test_chain_config_default_apipool_urls_is_none(self):
        from evm_chain_listener.models import ChainConfig
        cc = ChainConfig(name="eth", chain_id=1)
        assert cc.apipool_urls is None

    def test_existing_tests_still_work(self, tmp_path):
        """Verify config loading still works without apipool_urls field."""
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
        assert cfg.chains[0].apipool_urls is None
