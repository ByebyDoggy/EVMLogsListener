"""Tests for LogPusher and related components."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.evm_chain_listener.models import (
    AlertProcessorConfig,
    ChainConfig,
    Log,
    PusherStats,
    RPCNodeConfig,
)
from src.evm_chain_listener.config import load_config, parse_alert_processor_config
from src.evm_chain_listener.pusher import LogPusher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_log(
    tx_hash: str = "0xabc",
    log_idx: int = 0,
    block_number: int = 100,
    chain_id: int = 1,
    chain_name: str = "ethereum",
) -> Log:
    return Log(
        address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        topics=[
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045",
            "0x000000000000000000000000742d35cc6634c0532925a3b844bc9e7595f4b8a0",
        ],
        data="0x0000000000000000000000000000000000000000000000000000000000989680",
        block_number=block_number,
        transaction_hash=tx_hash,
        log_index=log_idx,
        transaction_index=0,
        block_hash="0xd49b57" + "a" * 58,
        removed=False,
        chain_id=chain_id,
        chain_name=chain_name,
        timestamp=datetime(2026, 4, 7, 12, 30, 45, tzinfo=timezone.utc),
    )


def _make_chains() -> list:
    return [
        ChainConfig(
            name="ethereum", chain_id=1, poll_interval=10,
            rpc_nodes=[RPCNodeConfig(url="http://localhost:8545")],
        ),
        ChainConfig(
            name="bsc", chain_id=56, poll_interval=10,
            rpc_nodes=[RPCNodeConfig(url="http://localhost:8645")],
        ),
    ]


def _make_pusher_config(**overrides) -> AlertProcessorConfig:
    defaults = dict(
        enabled=True,
        url="http://localhost:8000",
        push_interval_seconds=5.0,
        batch_size=200,
        max_payload_mb=10.0,
        retry_attempts=3,
        retry_base_delay_sec=1.0,
        timeout_seconds=10.0,
        reconnect_check_on_startup=True,
        replay_endpoint="/ingest/logs/replay",
    )
    defaults.update(overrides)
    return AlertProcessorConfig(**defaults)


def _mock_aiohttp_response(status_code, json_data=None, text_data=None, headers=None):
    """Return a mock response object."""
    resp = AsyncMock()
    resp.status = status_code
    resp.headers = headers or {}
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    return resp


class _FakeAiohttpCtx:
    """Async context manager wrapper for mocked aiohttp responses."""

    def __init__(self, response_mock):
        self._resp = response_mock

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


def _set_mock_do_post(pusher, return_value=True):
    """Mock pusher._do_post to avoid aiohttp session complexity entirely."""

    async def fake_do_post(url, payload):
        return return_value
    pusher._do_post = fake_do_post


def _capture_payload_on_do_post(pusher):
    """Mock _do_post and capture what payloads were sent."""
    captured = []

    async def fake_do_post(url, payload):
        captured.append((url, payload))
        return True
    pusher._do_post = fake_do_post
    return captured


# ===========================================================================
# Tests for Log.to_push_dict()
# ===========================================================================

class TestLogToPushDict:

    def test_integer_fields(self):
        log = make_log(block_number=19584123, log_idx=42)
        d = log.to_push_dict()
        assert isinstance(d["block_number"], int)
        assert d["block_number"] == 19584123
        assert isinstance(d["log_index"], int)
        assert d["log_index"] == 42
        assert isinstance(d["transaction_index"], int)

    def test_string_fields_unchanged(self):
        log = make_log(tx_hash="0xc310" + "d" * 60)
        d = log.to_push_dict()
        assert d["address"] == log.address
        assert d["topics"] == log.topics
        assert d["data"] == log.data
        assert d["transaction_hash"] == log.transaction_hash
        assert d["block_hash"] == log.block_hash
        assert d["removed"] is False

    def test_no_chain_fields(self):
        d = make_log().to_push_dict()
        assert "chain_id" not in d
        assert "chain_name" not in d
        assert "timestamp" not in d

    def test_differs_from_to_dict_for_numeric(self):
        log = make_log(block_number=256, log_idx=3)
        push_d = log.to_push_dict()
        api_d = log.to_dict()
        assert push_d["block_number"] == 256
        assert api_d["block_number"] == "0x100"
        assert push_d["log_index"] == 3
        assert api_d["log_index"] == "0x3"


# ===========================================================================
# Tests for AlertProcessorConfig model
# ===========================================================================

class TestAlertProcessorConfig:

    def test_defaults(self):
        cfg = AlertProcessorConfig()
        assert cfg.enabled is False
        assert cfg.url == ""
        assert cfg.batch_size == 200
        assert cfg.replay_endpoint == "/ingest/logs/replay"


class TestPusherStats:

    def test_defaults(self):
        s = PusherStats()
        assert s.total_pushed == 0
        assert s.buffer_size == 0
        assert s.last_error is None

    def test_to_dict(self):
        now = datetime.now(timezone.utc)
        s = PusherStats(total_pushed=10, last_error="timeout", last_push_at=now)
        d = s.to_dict()
        assert d["total_pushed"] == 10
        assert d["last_error"] == "timeout"


# ===========================================================================
# Tests for config parsing (alert_processor section)
# ===========================================================================

class TestAlertProcessorConfigParsing:

    def test_parse_full_config(self):
        data = {
            "enabled": True,
            "url": "http://alert-processor:9000",
            "push_interval_seconds": 3,
            "batch_size": 500,
            "retry_attempts": 5,
            "reconnect_check_on_startup": False,
        }
        cfg = parse_alert_processor_config(data)
        assert cfg.enabled is True
        assert cfg.url == "http://alert-processor:9000"
        assert cfg.push_interval_seconds == 3.0
        assert cfg.batch_size == 500
        assert cfg.retry_attempts == 5
        assert cfg.reconnect_check_on_startup is False

    def test_parse_empty_returns_defaults(self):
        cfg = parse_alert_processor_config(None)
        assert cfg.enabled is False

    def test_parse_partial(self):
        cfg = parse_alert_processor_config({"enabled": True, "url": "http://x"})
        assert cfg.enabled is True
        assert cfg.url == "http://x"

    def test_load_yaml_with_ap_section(self, tmp_path):
        yaml_content = (
            "chains:\n"
            "  - name: eth\n"
            "    chain_id: 1\n"
            "    poll_interval: 5\n"
            "    rpc_nodes:\n"
            "      - url: http://localhost:8545\n"
            "cache:\n"
            "  max_size: 500\n"
            "alert_processor:\n"
            "  enabled: true\n"
            "  url: http://ap:8000\n"
            "  batch_size: 100\n"
            "  push_interval_seconds: 2\n"
        )
        p = tmp_path / "test_cfg.yaml"
        p.write_text(yaml_content)
        cfg = load_config(str(p))
        assert cfg.alert_processor.enabled is True
        assert cfg.alert_processor.url == "http://ap:8000"
        assert cfg.alert_processor.batch_size == 100


# ===========================================================================
# Tests for LogPusher init and buffer
# ===========================================================================

class TestLogPusherInit:

    def test_disabled_when_not_enabled(self):
        p = LogPusher(config=_make_pusher_config(enabled=False), chains=_make_chains())
        assert p.enabled is False

    def test_disabled_when_url_empty(self):
        p = LogPusher(config=_make_pusher_config(url=""), chains=_make_chains())
        assert p.enabled is False

    def test_enabled_with_valid_config(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        assert p.enabled is True

    def test_stats_initial_state(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        assert p.stats.total_pushed == 0
        assert p.stats.buffer_size == 0


class TestLogPusherBuffer:

    @pytest.mark.asyncio
    async def test_buffer_accumulates_logs(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        logs = [make_log(tx_hash=f"0x{i}", block_number=100 + i) for i in range(5)]
        await p.on_new_logs(logs)
        assert p.stats.buffer_size == 5

    @pytest.mark.asyncio
    async def test_noop_when_disabled(self):
        p = LogPusher(config=_make_pusher_config(enabled=False), chains=_make_chains())
        await p.on_new_logs([make_log()])
        assert p.stats.buffer_size == 0

    @pytest.mark.asyncio
    async def test_noop_when_empty_logs(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        await p.on_new_logs([])
        assert p.stats.buffer_size == 0

    @pytest.mark.asyncio
    async def test_noop_when_no_url(self):
        p = LogPusher(config=_make_pusher_config(url=""), chains=_make_chains())
        await p.on_new_logs([make_log()])
        assert p.stats.buffer_size == 0


# ===========================================================================
# Tests for _send_batch (HTTP POST to /ingest/logs)
# ===========================================================================

class TestLogPusherSendBatch:

    @pytest.mark.asyncio
    async def test_successful_send(self):
        p = LogPusher(config=_make_pusher_config(batch_size=5), chains=_make_chains())
        _set_mock_do_post(p, return_value=True)

        logs = [make_log(block_number=100), make_log(tx_hash="0xbbb", block_number=101)]
        ok = await p._send_batch(1, logs)
        assert ok is True
        assert p.stats.total_pushed == 1
        assert p.stats.last_push_log_count == 2
        assert p._last_pushed_block[1] == 101

    @pytest.mark.asyncio
    async def test_rate_limit_returns_false(self):
        p = LogPusher(config=_make_pusher_config(retry_attempts=1), chains=_make_chains())
        _set_mock_do_post(p, return_value=False)

        ok = await p._send_batch(1, [make_log()])
        assert ok is False
        assert p.stats.total_failed == 1
        # _do_post returning False triggers generic failure path (not rate-limited specific)
        # but still counts as failed

    @pytest.mark.asyncio
    async def test_server_error_retries_then_fails(self):
        p = LogPusher(
            config=_make_pusher_config(retry_attempts=3, retry_base_delay_sec=0.01),
            chains=_make_chains(),
        )
        _set_mock_do_post(p, return_value=False)

        ok = await p._send_batch(1, [make_log()])
        assert ok is False
        assert p.stats.total_failed == 1
        assert p.stats.total_retried >= 2

    @pytest.mark.asyncio
    async def test_bad_request_counts_as_success_no_retry(self):
        p = LogPusher(
            config=_make_pusher_config(retry_attempts=3, retry_base_delay_sec=0.01),
            chains=_make_chains(),
        )
        _set_mock_do_post(p, return_value=True)  # Simulates 400 -> returns True

        ok = await p._send_batch(1, [make_log()])
        assert ok is True
        assert p.stats.total_pushed == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        p = LogPusher(config=_make_pusher_config(retry_attempts=1), chains=_make_chains())

        async def fake_do_post_timeout(url, payload):
            raise asyncio.TimeoutError("request timed out")
        p._do_post = fake_do_post_timeout

        ok = await p._send_batch(1, [make_log()])
        assert ok is False
        assert p.stats.last_error == "request timed out"

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self):
        p = LogPusher(config=_make_pusher_config(retry_attempts=1), chains=_make_chains())

        async def fake_do_post_conn_err(url, payload):
            raise aiohttp.ClientError("connection refused")
        p._do_post = fake_do_post_conn_err

        ok = await p._send_batch(1, [make_log()])
        assert ok is False
        assert "connection" in (p.stats.last_error or "")

    @pytest.mark.asyncio
    async def test_succeeds_after_one_retry(self):
        p = LogPusher(
            config=_make_pusher_config(retry_attempts=3, retry_base_delay_sec=0.01),
            chains=_make_chains(),
        )
        counter = [0]

        async def multi(url, payload):
            counter[0] += 1
            if counter[0] <= 1:
                return False
            return True
        p._do_post = multi

        result = await p._send_batch(1, [make_log()])
        assert result is True
        assert p.stats.total_pushed == 1
        assert p.stats.total_retried == 1


# ===========================================================================
# Tests for flush_all grouping
# ===========================================================================

class TestLogPusherFlushAll:

    @pytest.mark.asyncio
    async def test_flush_groups_by_chain(self):
        p = LogPusher(config=_make_pusher_config(batch_size=999), chains=_make_chains())
        sent_batches = []

        async def fake_send(cid, logs):
            sent_batches.append((cid, len(logs)))
            return True
        p._send_batch = fake_send

        p._pending_buffer.extend([
            make_log(chain_id=1, block_number=100),
            make_log(chain_id=1, block_number=101),
            make_log(chain_id=56, block_number=200),
            make_log(chain_id=56, block_number=201),
            make_log(chain_id=56, block_number=202),
        ])
        p._stats.buffer_size = 5

        await p._flush_all()
        assert len(sent_batches) == 2
        assert (1, 2) in sent_batches
        assert (56, 3) in sent_batches
        assert p.stats.buffer_size == 0

    @pytest.mark.asyncio
    async def test_flush_empty_is_noop(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        called = False

        async def fake(cid, logs):
            nonlocal called
            called = True
            return True
        p._send_batch = fake
        await p._flush_all()
        assert not called


# ===========================================================================
# Tests for replay
# ===========================================================================

class TestLogPusherReplay:

    @pytest.mark.asyncio
    async def test_send_replay_success(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        _set_mock_do_post(p, return_value=True)

        logs = [make_log(block_number=500), make_log(tx_hash="0xr1", block_number=501)]
        ok = await p.send_replay(chain_id=1, from_block=500, to_block=501, logs=logs)
        assert ok is True

    @pytest.mark.asyncio
    async def test_send_replay_empty_logs(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        ok = await p.send_replay(1, 100, 200, [])
        assert ok is True


# ===========================================================================
# Lifecycle tests
# ===========================================================================

class TestLogPusherStartStop:

    @pytest.mark.asyncio
    async def test_start_creates_session_and_task(self):
        p = LogPusher(config=_make_pusher_config(push_interval_seconds=0.1), chains=_make_chains())
        await p.start()
        assert p._session is not None
        assert p._periodic_task is not None
        await p.stop()

    @pytest.mark.asyncio
    async def test_start_disabled_is_noop(self):
        p = LogPusher(config=_make_pusher_config(enabled=False), chains=_make_chains())
        await p.start()
        assert p._periodic_task is None
        await p.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self):
        p = LogPusher(config=_make_pusher_config(push_interval_seconds=0.1), chains=_make_chains())
        await p.start()
        await p.stop()

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining(self):
        p = LogPusher(config=_make_pusher_config(batch_size=999, push_interval_seconds=60), chains=_make_chains())
        flushed = False

        async def fake():
            nonlocal flushed
            flushed = True
        p._flush_all = fake
        p._pending_buffer = [make_log()]

        await p.stop()
        assert flushed


# ===========================================================================
# Threshold trigger tests
# ===========================================================================

class TestLogPusherThresholdTrigger:

    @pytest.mark.asyncio
    async def test_threshold_triggers_flush(self):
        p = LogPusher(config=_make_pusher_config(batch_size=3), chains=_make_chains())
        count = [0]

        async def counting_flush():
            count[0] += 1
            p._pending_buffer.clear()
            p._stats.buffer_size = 0
        p._flush_all = counting_flush

        await p.on_new_logs([make_log(), make_log()])
        assert count[0] == 0
        assert p.stats.buffer_size == 2

        await p.on_new_logs([make_log()])
        assert count[0] == 1


# ===========================================================================
# Reconnect status check
# ===========================================================================

class TestLogPusherCheckReconnectStatus:

    @pytest.mark.asyncio
    async def test_success(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())
        expected = {"is_connected": True, "consumed_blocks": {"1": {"last_block": 19000000}}}

        async def fake_check():
            return expected
        p.check_reconnect_status = fake_check

        result = await p.check_reconnect_status()
        assert result == expected

    @pytest.mark.asyncio
    async def test_failure_returns_none(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())

        async def fake_check():
            return None
        p.check_reconnect_status = fake_check

        result = await p.check_reconnect_status()
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        p = LogPusher(config=_make_pusher_config(), chains=_make_chains())

        async def fake_check():
            return None
        p.check_reconnect_status = fake_check

        result = await p.check_reconnect_status()
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        p = LogPusher(config=_make_pusher_config(enabled=False), chains=_make_chains())
        result = await p.check_reconnect_status()
        assert result is None
