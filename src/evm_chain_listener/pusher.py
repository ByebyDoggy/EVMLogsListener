"""LogPusher - pushes logs from EVMLogListener to AlertProcessor."""

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

from .models import AlertProcessorConfig, ChainConfig, Log, PusherStats
from .utils.logging import get_logger

logger = get_logger(__name__)


class LogPusher:
    """Incremental log pusher that sends buffered logs to AlertProcessor.

    Supports dual-trigger strategy:
      - Timer-based: flush every N seconds (push_interval_seconds)
      - Threshold-based: flush when buffer >= M logs (batch_size)

    Whichever triggers first causes a push.  Logs are grouped by chain
    before sending so each POST request covers a single chain.

    On failure the data stays in the buffer and is retried on next flush.
    """

    def __init__(
        self,
        config: AlertProcessorConfig,
        chains: List[ChainConfig],
    ):
        self._config = config
        # Map chain_id -> chain_name for payload construction
        self._chain_names: Dict[int, str] = {c.chain_id: c.name for c in chains}

        self._pending_buffer: List[Log] = []
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._stats = PusherStats()
        # Track last successfully pushed block per chain_id for gap detection
        self._last_pushed_block: Dict[int, int] = {}
        self._periodic_task: Optional[asyncio.Task] = None
        _running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def stats(self) -> PusherStats:
        return self._stats

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.url)

    async def start(self) -> None:
        """Start the periodic flush background task."""
        if not self.enabled:
            logger.info("LogPusher disabled (alert_processor.enabled=false or url not set)")
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
        )
        self._periodic_task = asyncio.create_task(self._periodic_flush())
        logger.info(
            "LogPusher started",
            extra={
                "target_url": self._config.url,
                "interval_sec": self._config.push_interval_seconds,
                "batch_size": self._config.batch_size,
            },
        )

    async def stop(self) -> None:
        """Stop pusher and flush any remaining logs."""
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
        # Final flush of remaining buffer
        if self._pending_buffer:
            logger.info("LogPusher stopping, flushing remaining %d logs", len(self._pending_buffer))
            await self._flush_all()
        if self._session and not self._session.closed:
            await self._session.close()

    async def on_new_logs(self, logs: List[Log]) -> None:
        """Called by ChainListener / BacktestRunner when new logs are received."""
        if not self.enabled or not logs:
            return
        async with self._lock:
            self._pending_buffer.extend(logs)
            self._stats.buffer_size = len(self._pending_buffer)
        # Threshold trigger: flush immediately when batch size reached
        if len(self._pending_buffer) >= self._config.batch_size:
            await self._flush_all()

    async def check_reconnect_status(self) -> Optional[Dict[str, Any]]:
        """Call GET /ingest/status on AlertProcessor to detect gaps.

        Returns the JSON response dict or None on failure.
        """
        if not self.enabled or not self._session:
            return None
        try:
            url = f"{self._config.url.rstrip('/')}/ingest/status"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        "AlertProcessor status checked",
                        extra={"status": data},
                    )
                    return data
                else:
                    logger.warning(
                        "AlertProcessor status returned status %d",
                        resp.status,
                    )
        except Exception as e:
            logger.warning("Failed to check AlertProcessor reconnect status: %s", e)
        return None

    async def send_replay(
        self,
        chain_id: int,
        from_block: int,
        to_block: int,
        logs: List[Log],
    ) -> bool:
        """Send a replay (gap-fill) request via POST /ingest/logs/replay."""
        if not self.enabled or not logs:
            return True
        chain_name = self._chain_names.get(chain_id, f"chain_{chain_id}")
        payload = {
            "chain_id": chain_id,
            "from_block": from_block,
            "to_block": to_block,
            "reason": "reconnection_gap",
            "logs": [log.to_push_dict() for log in logs],
        }
        try:
            ok = await self._do_post(f"{self._config.url.rstrip('/')}{self._config.replay_endpoint}", payload)
            if ok:
                logger.info(
                    "[replay] Sent replay for chain=%s blocks=[%d,%d] count=%d",
                    chain_name, from_block, to_block, len(logs),
                    extra={
                        "chain": chain_name,
                        "from_block": from_block,
                        "to_block": to_block,
                        "log_count": len(logs),
                    },
                )
            return ok
        except Exception as e:
            logger.error("[replay] Failed: %s", e, extra={"chain": chain_name})
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _periodic_flush(self):
        """Background loop that flushes on timer interval."""
        while True:
            await asyncio.sleep(self._config.push_interval_seconds)
            async with self._lock:
                has_pending = len(self._pending_buffer) > 0
            if has_pending:
                await self._flush_all()

    async def _flush_all(self) -> None:
        """Flush all pending logs grouped by chain."""
        async with self._lock:
            if not self._pending_buffer:
                return
            # Take all pending logs out
            pending = list(self._pending_buffer)
            self._pending_buffer.clear()
            self._stats.buffer_size = 0

        # Group by chain_id
        by_chain: Dict[int, List[Log]] = defaultdict(list)
        for log in pending:
            cid = log.chain_id or 0
            by_chain[cid].append(log)

        for chain_id, chain_logs in sorted(by_chain.items()):
            await self._send_batch(chain_id, chain_logs)

    async def _send_batch(self, chain_id: int, logs: List[Log]) -> bool:
        """Build payload and POST to AlertProcessor's /ingest/logs endpoint."""
        if not logs:
            return True

        chain_name = self._chain_names.get(chain_id, f"chain_{chain_id}")
        block_nums = [l.block_number for l in logs]
        from_block = min(block_nums)
        to_block = max(block_nums)

        payload = {
            "chain_id": chain_id,
            "chain_name": chain_name,
            "logs": [log.to_push_dict() for log in logs],
            "from_block": from_block,
            "to_block": to_block,
            "log_count": len(logs),
            "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") +
                         f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
        }

        url = f"{self._config.url.rstrip('/')}/ingest/logs"
        success = await self._do_post_with_retry(url, payload, chain_name, from_block, to_block, len(logs))

        if success:
            self._last_pushed_block[chain_id] = to_block
        return success

    async def _do_post_with_retry(
        self,
        url: str,
        payload: dict,
        chain_name: str,
        from_block: int,
        to_block: int,
        log_count: int,
    ) -> bool:
        """POST with retry + backoff per spec Section 8."""
        base_delay = self._config.retry_base_delay_sec
        max_attempts = self._config.retry_attempts

        for attempt in range(max_attempts):
            try:
                result = await self._do_post(url, payload)
            except Exception as e:
                self._stats.last_error = str(e)
                logger.warning(
                    f"[pusher] _do_post error: {e}",
                    extra={"chain": chain_name},
                )
                if attempt < max_attempts - 1:
                    delay = base_delay * (2 ** attempt)
                    self._stats.total_retried += 1
                    logger.warning(
                        "[pusher] Push error, retrying in %.1fs (attempt %d/%d)",
                        delay, attempt + 1, max_attempts,
                        extra={"chain": chain_name},
                    )
                    await asyncio.sleep(delay)
                    continue
                # Final attempt also failed with exception
                break

            if result:
                now = datetime.now(timezone.utc)
                self._stats.total_pushed += 1
                self._stats.last_push_at = now
                self._stats.last_push_log_count = log_count
                self._stats.last_error = None
                logger.info(
                    "[pusher] Pushed %d logs for chain %s "
                    "(blocks %d-%d)",
                    log_count, chain_name, from_block, to_block,
                    extra={
                        "chain": chain_name,
                        "from_block": from_block,
                        "to_block": to_block,
                        "log_count": log_count,
                    },
                )
                return True
            # Non-retryable or final attempt
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                self._stats.total_retried += 1
                logger.warning(
                    "[pusher] Push failed, retrying in %.1fs (attempt %d/%d)",
                    delay, attempt + 1, max_attempts,
                    extra={"chain": chain_name},
                )
                await asyncio.sleep(delay)

        self._stats.total_failed += 1
        logger.error(
            "[pusher] All retries exhausted for chain %s (blocks %d-%d)",
            chain_name, from_block, to_block,
            extra={"chain": chain_name, "from_block": from_block, "to_block": to_block},
        )
        return False

    async def _do_post(self, url: str, payload: dict) -> bool:
        """Single HTTP POST attempt. Returns True on accepted/success."""
        if not self._session:
            return False
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status in (200, 202):
                    return True
                elif resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    self._stats.last_error = f"rate_limited(retry_after={retry_after})"
                    logger.warning(
                        "[pusher] Rate limited, Retry-After=%ds", retry_after,
                    )
                    # Return False so caller can handle retry/BackOff
                    return False
                elif resp.status == 400:
                    body = await resp.text()
                    self._stats.last_error = f"bad_request({resp.status}): {body[:200]}"
                    logger.error(
                        "[pusher] Bad request (400): %s", body[:300],
                    )
                    # Per spec: 400 means format issue, discard this batch
                    return True  # Don't keep retrying a bad-format batch
                elif 500 <= resp.status < 600:
                    body = await resp.text()
                    self._stats.last_error = f"server_error({resp.status})"
                    logger.error(
                        "[pusher] Server error %d: %s", resp.status, body[:200],
                    )
                    return False
                else:
                    self._stats.last_error = f"http_{resp.status}"
                    logger.warning("[pusher] Unexpected status %d", resp.status)
                    return False
        except asyncio.TimeoutError:
            self._stats.last_error = "timeout"
            logger.warning("[pusher] Request timed out")
            return False
        except aiohttp.ClientError as e:
            self._stats.last_error = f"connection_error({e})"
            logger.warning("[pusher] Connection error: %s", e)
            return False
        except Exception as e:
            self._stats.last_error = str(e)
            logger.error("[pusher] Unexpected error: %s", e)
            return False
