"""Base chain listener implementation."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..models import ChainConfig, ChainStatus, Log
from ..rpc.binary_search import BinarySearchQuerier
from ..rpc.exceptions import AllNodesFailedError
from ..rpc.apipool_client import EvmRpcPool
from ..utils.logging import get_logger

logger = get_logger(__name__)


class ChainListener:
    """Listens to a specific blockchain for new logs.
    
    Uses :class:`~evm_chain_listener.rpc.apipool_client.EvmRpcPool`
    which delegates all rotation/retry/ban logic to apipool-server (or the
    local apipool-ng library).  No client-side polling or back-off state.
    """
    
    def __init__(
        self,
        config: ChainConfig,
        log_callback: Callable[[List[Log]], None],
    ):
        """Initialize the chain listener.
        
        Args:
            config: Chain configuration
            log_callback: Callback function to receive new logs
        """
        self.config = config
        self.name = config.name
        self.chain_id = config.chain_id
        self.poll_interval = config.poll_interval
        self.log_callback = log_callback
        
        # RPC pool will be set during async init
        self._rpc_pool: Optional[EvmRpcPool] = None
        self._querier: Optional[BinarySearchQuerier] = None
        
        self._last_block: Optional[int] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None
        self._error: Optional[str] = None
        self._start_time: Optional[datetime] = None
    
    @property
    def last_block(self) -> Optional[int]:
        """Get the last processed block number."""
        return self._last_block
    
    @property
    def status(self) -> ChainStatus:
        """Get the current status of the listener."""
        return ChainStatus(
            name=self.name,
            chain_id=self.chain_id,
            status="running" if self._running else "stopped",
            last_block=self._last_block,
            last_poll=self._last_poll,
            error=self._error,
        )
    
    async def _init_rpc_pool(self) -> EvmRpcPool:
        """Build or connect the RPC pool from config.
        
        If ``apipool_server`` is configured, uses server-driven mode via
        ``EvmRpcPool.from_server()``.  Otherwise falls back to a static pool
        from local ``rpc_nodes`` URLs.
        """
        if self.config.apipool_server:
            srv = self.config.apipool_server
            logger.info(
                "Initializing RPC pool from server: %s (pool=%s)",
                srv["service_url"], srv.get("pool_identifier", srv.get("client_type")),
                extra={"chain": self.name},
            )
            pool = await EvmRpcPool.from_server(
                service_url=srv["service_url"],
                pool_identifier=srv.get("pool_identifier", srv.get("client_type", "")),
                username=srv["username"],
                password=srv["password"],
                chain_id=self.chain_id,
                chain_name=self.name,
            )
        else:
            urls = [n.url for n in self.config.rpc_nodes]
            if not urls:
                raise RuntimeError(
                    f"Chain '{self.name}': no rpc_nodes and no apipool_server configured"
                )
            pool = EvmRpcPool(
                urls=urls,
                chain_id=self.chain_id,
                chain_name=self.name,
            )
        
        # Start background refresh in server mode
        await pool.start()
        return pool
    
    async def start(self) -> None:
        """Start the chain listener."""
        if self._running:
            logger.warning(f"Chain listener {self.name} is already running")
            return
        
        self._running = True
        self._start_time = datetime.now(timezone.utc)
        self._error = None
        
        logger.info(
            f"Starting chain listener: {self.name} (chain_id={self.chain_id})",
            extra={"chain": self.name},
        )
        
        # Initialize RPC pool (async — may contact server)
        self._rpc_pool = await self._init_rpc_pool()
        self._querier = BinarySearchQuerier(self._rpc_pool)
        
        self._task = asyncio.create_task(self._poll_loop())
    
    async def stop(self) -> None:
        """Stop the chain listener."""
        if not self._running:
            return
        
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        if self._rpc_pool:
            await self._rpc_pool.stop()
        
        logger.info(
            f"Stopped chain listener: {self.name}, last_block={self._last_block}",
            extra={"chain": self.name},
        )
    
    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._poll()
            except AllNodesFailedError as e:
                self._error = str(e)
                logger.error(
                    f"All RPC nodes failed for {self.name}, retrying in {self.poll_interval}s",
                    extra={"chain": self.name},
                )
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error = str(e)
                logger.error(
                    f"Poll error for {self.name}: {e}",
                    extra={"chain": self.name},
                )
                await asyncio.sleep(self.poll_interval)
    
    async def _poll(self) -> None:
        """Poll for new logs."""
        start_time = time.time()
        
        try:
            current_block = await self._rpc_pool.get_block_number()
        except Exception as e:
            logger.error(
                f"Failed to get current block for {self.name}: {e}",
                extra={"chain": self.name},
            )
            raise
        
        if self._last_block is None:
            self._last_block = current_block
            logger.info(
                f"Initialized {self.name} at block {current_block}",
                extra={"chain": self.name},
            )
            return
        
        if current_block <= self._last_block:
            await asyncio.sleep(self.poll_interval)
            return
        
        from_block = self._last_block + 1
        to_block = current_block
        
        try:
            logs = await self._querier.query_logs(
                from_block=from_block,
                to_block=to_block,
                address=self.config.address_filter[0] if self.config.address_filter else None,
                topics=self.config.topics_filter if self.config.topics_filter else None,
            )
        except Exception as e:
            logger.error(
                f"Failed to query logs for {self.name}: {e}",
                extra={
                    "chain": self.name,
                    "from_block": from_block,
                    "to_block": to_block,
                },
            )
            raise
        
        self._last_block = to_block
        self._last_poll = datetime.now(timezone.utc)
        self._error = None
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        if logs:
            logger.info(
                f"Received {len(logs)} logs from {self.name}",
                extra={
                    "chain": self.name,
                    "from_block": from_block,
                    "to_block": to_block,
                    "log_count": len(logs),
                    "duration_ms": duration_ms,
                },
            )
            self.log_callback(logs)
        else:
            logger.debug(
                f"No logs in {self.name} blocks {from_block}-{to_block}",
                extra={
                    "chain": self.name,
                    "from_block": from_block,
                    "to_block": to_block,
                    "duration_ms": duration_ms,
                },
            )
    
    async def health_check(self) -> bool:
        """Perform a health check on the RPC pool.
        
        Returns:
            True if healthy, False otherwise
        """
        try:
            await self._rpc_pool.get_block_number()
            return True
        except Exception:
            return False
