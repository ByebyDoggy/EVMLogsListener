"""Backtest mode for querying historical logs."""

import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..models import (
    BacktestConfig,
    BacktestResult,
    BacktestTaskStatus,
    ChainConfig,
    Log,
)
from ..rpc.binary_search import BinarySearchQuerier
from ..rpc.exceptions import AllNodesFailedError
from ..rpc.node_pool import RPCNodePool
from ..utils.logging import get_logger

logger = get_logger(__name__)


class BacktestRunner:
    """Executes backtest queries on historical block ranges.

    Uses the same RPC pool and BinarySearchQuerier as realtime mode,
    but queries a fixed [from_block, to_block] range in batches.

    When ``use_rpc=False``, no RPC pool is created (used for file-based
    replay mode where logs come from local recordings instead of RPC).
    """

    def __init__(
        self,
        chain_config: ChainConfig,
        log_callback: Callable[[List[Log]], None],
        use_rpc: bool = True,
    ):
        self.chain_config = chain_config
        self.log_callback = log_callback
        self._use_rpc = use_rpc

        if use_rpc:
            self._rpc_pool = RPCNodePool(
                nodes=chain_config.rpc_nodes,
                chain_id=chain_config.chain_id,
                chain_name=chain_config.name,
            )
            self._querier = BinarySearchQuerier(self._rpc_pool)
        else:
            self._rpc_pool = None
            self._querier = None

        # Active task tracking
        self._tasks: Dict[str, BacktestTaskStatus] = {}

    @property
    def rpc_pool(self) -> Optional[RPCNodePool]:
        return self._rpc_pool

    async def close(self) -> None:
        if self._rpc_pool:
            await self._rpc_pool.close()

    async def health_check(self) -> bool:
        if not self._rpc_pool:
            # File replay mode: always "healthy" since no RPC needed
            return True
        try:
            await self._rpc_pool.get_block_number()
            return True
        except Exception:
            return False

    def resolve_chain(self, chain_name_or_id: str) -> Optional[ChainConfig]:
        """Check if this runner matches the requested chain."""
        if str(self.chain_config.chain_id) == chain_name_or_id:
            return self.chain_config
        if self.chain_config.name.lower() == chain_name_or_id.lower():
            return self.chain_config
        return None

    async def submit_backtest(
        self,
        config: BacktestConfig,
    ) -> BacktestTaskStatus:
        """Submit a backtest task and start execution.

        Args:
            config: Backtest configuration with from/to block range

        Returns:
            Task status that can be polled for progress
        """
        if not self._use_rpc:
            raise RuntimeError(
                "Cannot submit RPC backtest when runner is in file-replay mode "
                "(use_rpc=False). Use the replay config instead."
            )

        task_id = uuid.uuid4().hex[:12]

        # Validate range
        if config.from_block > config.to_block:
            raise ValueError(
                f"from_block ({config.from_block}) must be <= to_block ({config.to_block})"
            )

        total_blocks = config.to_block - config.from_block + 1
        total_batches = (total_blocks + config.batch_size - 1) // config.batch_size

        task_status = BacktestTaskStatus(
            task_id=task_id,
            chain_name=self.chain_config.name,
            chain_id=self.chain_config.chain_id,
            from_block=config.from_block,
            to_block=config.to_block,
            status="pending",
            total_batches=total_batches,
            started_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = task_status

        logger.info(
            f"Backtest task {task_id} submitted: "
            f"chain={self.chain_config.name}, "
            f"blocks=[{config.from_block}, {config.to_block}], "
            f"batch_size={config.batch_size}, "
            f"total_batches={total_batches}",
        )

        # Run asynchronously in background
        import asyncio
        asyncio.create_task(
            self._execute_backtest(task_id, config, task_status)
        )

        return task_status

    async def _execute_backtest(
        self,
        task_id: str,
        config: BacktestConfig,
        task_status: BacktestTaskStatus,
    ) -> BacktestResult:
        """Execute the backtest query batch by batch."""
        start_time = time.time()
        task_status.status = "running"
        total_logs_found = 0
        completed_batches = 0

        address_filter = (
            self.chain_config.address_filter[0]
            if self.chain_config.address_filter
            else None
        )
        topics_filter = (
            self.chain_config.topics_filter
            if self.chain_config.topics_filter
            else None
        )

        current = config.from_block

        try:
            while current <= config.to_block:
                batch_end = min(current + config.batch_size - 1, config.to_block)
                task_status.progress_current_block = current

                logger.info(
                    f"[{task_id}] Querying batch: blocks [{current}, {batch_end}]",
                    extra={
                        "chain": self.chain_config.name,
                        "from_block": current,
                        "to_block": batch_end,
                    },
                )

                try:
                    logs = await self._querier.query_logs(
                        from_block=current,
                        to_block=batch_end,
                        address=address_filter,
                        topics=topics_filter,
                    )
                except AllNodesFailedError as e:
                    logger.error(
                        f"[{task_id}] All RPC nodes failed at block {current}: {e}",
                        extra={"chain": self.chain_config.name},
                    )
                    raise

                completed_batches += 1
                task_status.completed_batches = completed_batches

                if logs:
                    total_logs_found += len(logs)
                    task_status.logs_found = total_logs_found
                    self.log_callback(logs)
                    logger.info(
                        f"[{task_id}] Batch complete: {len(logs)} logs found",
                        extra={
                            "chain": self.chain_config.name,
                            "from_block": current,
                            "to_block": batch_end,
                            "log_count": len(logs),
                        },
                    )
                else:
                    logger.debug(
                        f"[{task_id}] No logs in blocks [{current}, {batch_end}]",
                        extra={"chain": self.chain_config.name},
                    )

                current = batch_end + 1

            duration = time.time() - start_time
            task_status.status = "completed"
            task_status.completed_at = datetime.now(timezone.utc)

            result = BacktestResult(
                chain_name=self.chain_config.name,
                chain_id=self.chain_config.chain_id,
                from_block=config.from_block,
                to_block=config.to_block,
                total_logs_found=total_logs_found,
                total_batches=completed_batches,
                duration_seconds=duration,
                status="completed",
            )

            logger.info(
                f"[{task_id}] Backtest COMPLETED: "
                f"{total_logs_found} logs in {completed_batches} batches, "
                f"{duration:.2f}s",
                extra={
                    "chain": self.chain_config.name,
                    "log_count": total_logs_found,
                    "duration_ms": int(duration * 1000),
                },
            )

            return result

        except Exception as e:
            duration = time.time() - start_time
            task_status.status = "failed"
            task_status.error = str(e)
            task_status.completed_at = datetime.now(timezone.utc)

            logger.error(
                f"[{task_id}] Backtest FAILED after {duration:.2f}s: {e}",
                extra={
                    "chain": self.chain_config.name,
                    "error": str(e),
                },
            )

            return BacktestResult(
                chain_name=self.chain_config.name,
                chain_id=self.chain_config.chain_id,
                from_block=config.from_block,
                to_block=config.to_block,
                total_logs_found=total_logs_found,
                total_batches=completed_batches,
                duration_seconds=duration,
                status="failed",
                error=str(e),
            )

    def get_task_status(self, task_id: str) -> Optional[BacktestTaskStatus]:
        """Get the status of a backtest task."""
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[BacktestTaskStatus]:
        """List all known backtest tasks."""
        return list(self._tasks.values())
