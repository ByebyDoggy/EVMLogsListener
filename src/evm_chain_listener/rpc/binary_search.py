"""Binary search query for handling log limit exceeded errors."""

import asyncio
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .apipool_client import EvmRpcPool

from ..models import Log
from ..utils.logging import get_logger
from .exceptions import AllNodesFailedError, LogLimitExceededError

logger = get_logger(__name__)


class BinarySearchQuerier:
    """Handles log queries with binary search to handle limit exceeded errors.
    
    Works with any RPC pool that exposes ``get_logs()`` returning raw dicts.
    """
    
    MAX_LOGS_PER_QUERY = 10000
    
    def __init__(
        self,
        rpc_pool: "EvmRpcPool",
        max_results_per_query: int = MAX_LOGS_PER_QUERY,
    ):
        """Initialize the binary search querier.
        
        Args:
            rpc_pool: RPC node pool to use for queries
            max_results_per_query: Maximum results per query before splitting
        """
        self._rpc_pool = rpc_pool
        self._max_results_per_query = max_results_per_query
    
    async def query_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
    ) -> List[Log]:
        """Query logs with automatic binary search on limit exceeded.
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
            address: Contract address to filter
            topics: Event topics to filter
        
        Returns:
            List of Log objects
        """
        return await self._query_with_retry(
            from_block=from_block,
            to_block=to_block,
            address=address,
            topics=topics,
        )
    
    async def _query_with_retry(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
        depth: int = 0,
    ) -> List[Log]:
        """Query logs with retry on rate limit errors.
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
            address: Contract address to filter
            topics: Event topics to filter
            depth: Current recursion depth for logging
        
        Returns:
            List of Log objects
        """
        max_retries = 3
        base_delay = 1.0
        
        last_error = None
        for attempt in range(max_retries):
            try:
                return await self._do_query(
                    from_block=from_block,
                    to_block=to_block,
                    address=address,
                    topics=topics,
                    depth=depth,
                )
            except LogLimitExceededError:
                raise
            except AllNodesFailedError:
                raise
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"Query failed, retrying in {delay}s: {e}",
                        extra={"chain": self._rpc_pool.chain_name},
                    )
                    await asyncio.sleep(delay)
                    continue
        
        raise last_error or Exception("Query failed after retries")
    
    async def _do_query(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
        depth: int = 0,
    ) -> List[Log]:
        """Execute a single query or split if limit exceeded.
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
            address: Contract address to filter
            topics: Event topics to filter
            depth: Current recursion depth for logging
        
        Returns:
            List of Log objects
        """
        try:
            raw_logs = await self._rpc_pool.get_logs(
                from_block=from_block,
                to_block=to_block,
                address=address,
                topics=topics,
            )

            # Convert raw RPC dicts -> Log objects
            logs = [
                Log.from_rpc_response(item, self._rpc_pool.chain_id, self._rpc_pool.chain_name)
                for item in raw_logs
            ]

            if len(logs) >= self._max_results_per_query:
                logger.warning(
                    f"Query returned {len(logs)} logs, approaching limit",
                    extra={
                        "chain": self._rpc_pool.chain_name,
                        "from_block": from_block,
                        "to_block": to_block,
                    },
                )

            return logs
        
        except LogLimitExceededError:
            if from_block == to_block:
                logger.warning(
                    f"Single block still exceeds limit, returning empty: block {from_block}",
                    extra={
                        "chain": self._rpc_pool.chain_name,
                        "from_block": from_block,
                    },
                )
                return []
            
            mid = (from_block + to_block) // 2
            
            logger.info(
                f"Splitting query: [{from_block}, {to_block}] -> [{from_block}, {mid}] + [{mid + 1}, {to_block}]",
                extra={
                    "chain": self._rpc_pool.chain_name,
                    "depth": depth,
                },
            )
            
            left_task = self._query_with_retry(
                from_block=from_block,
                to_block=mid,
                address=address,
                topics=topics,
                depth=depth + 1,
            )
            
            right_task = self._query_with_retry(
                from_block=mid + 1,
                to_block=to_block,
                address=address,
                topics=topics,
                depth=depth + 1,
            )
            
            left_logs, right_logs = await asyncio.gather(left_task, right_task)
            
            return left_logs + right_logs
