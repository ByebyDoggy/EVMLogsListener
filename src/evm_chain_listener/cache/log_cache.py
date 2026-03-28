"""Log cache for storing and querying logs."""

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..models import CacheStats, CacheConfig, Log, PaginatedResult
from ..utils.logging import get_logger

logger = get_logger(__name__)


class LogCache:
    """Thread-safe in-memory cache for blockchain logs with FIFO eviction."""
    
    def __init__(self, config: Optional[CacheConfig] = None):
        """Initialize the log cache.
        
        Args:
            config: Cache configuration
        """
        config = config or CacheConfig()
        self._max_size = config.max_size
        self._cache: deque = deque(maxlen=self._max_size)
        self._index: Dict[str, Log] = {}
        self._lock = threading.RLock()
        self._total_added = 0
        self._total_dropped = 0
    
    @property
    def max_size(self) -> int:
        """Get maximum cache size."""
        return self._max_size
    
    @property
    def current_size(self) -> int:
        """Get current cache size."""
        return len(self._cache)
    
    @property
    def total_added(self) -> int:
        """Get total number of logs ever added."""
        return self._total_added
    
    @property
    def total_dropped(self) -> int:
        """Get total number of logs dropped due to size limit."""
        return self._total_dropped
    
    def add(self, log: Log) -> bool:
        """Add a log to the cache.
        
        Args:
            log: Log to add
        
        Returns:
            True if log was added, False if duplicate
        """
        key = log.unique_key
        
        with self._lock:
            if key in self._index:
                return False
            
            while len(self._cache) >= self._max_size:
                old_log = self._cache.popleft()
                old_key = old_log.unique_key
                del self._index[old_key]
                self._total_dropped += 1
                logger.debug(f"Cache full, dropped old log: {old_key}")
            
            self._cache.append(log)
            self._index[key] = log
            self._total_added += 1
            
            return True
    
    def add_many(self, logs: List[Log]) -> int:
        """Add multiple logs to the cache.
        
        Args:
            logs: List of logs to add
        
        Returns:
            Number of logs actually added (excluding duplicates)
        """
        added_count = 0
        for log in logs:
            if self.add(log):
                added_count += 1
        return added_count
    
    def query(
        self,
        chain_id: Optional[int] = None,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        address: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> PaginatedResult:
        """Query logs from the cache.
        
        Args:
            chain_id: Filter by chain ID
            from_block: Filter by minimum block number
            to_block: Filter by maximum block number
            from_time: Filter by minimum timestamp
            to_time: Filter by maximum timestamp
            address: Filter by contract address
            page: Page number (1-indexed)
            page_size: Number of items per page
        
        Returns:
            Paginated result of logs
        """
        with self._lock:
            filtered = list(self._cache)
            
            if chain_id is not None:
                filtered = [log for log in filtered if log.chain_id == chain_id]
            
            if from_block is not None:
                filtered = [log for log in filtered if log.block_number >= from_block]
            
            if to_block is not None:
                filtered = [log for log in filtered if log.block_number <= to_block]
            
            if from_time is not None:
                filtered = [log for log in filtered 
                           if log.timestamp is not None and log.timestamp >= from_time]
            
            if to_time is not None:
                filtered = [log for log in filtered 
                           if log.timestamp is not None and log.timestamp <= to_time]
            
            if address is not None:
                filtered = [log for log in filtered if log.address.lower() == address.lower()]
            
            filtered.sort(key=lambda log: (log.block_number, log.log_index), reverse=True)
            
            total = len(filtered)
            
            if page_size > 1000:
                page_size = 1000
            if page_size < 1:
                page_size = 100
            if page < 1:
                page = 1
            
            start = (page - 1) * page_size
            end = start + page_size
            
            items = filtered[start:end]
            
            return PaginatedResult(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
            )
    
    def get_stats(self) -> CacheStats:
        """Get cache statistics.
        
        Returns:
            CacheStats object with statistics
        """
        with self._lock:
            by_chain: Dict[int, int] = {}
            oldest: Optional[datetime] = None
            newest: Optional[datetime] = None
            
            for log in self._cache:
                chain_id = log.chain_id or 0
                by_chain[chain_id] = by_chain.get(chain_id, 0) + 1
                
                if log.timestamp:
                    if oldest is None or log.timestamp < oldest:
                        oldest = log.timestamp
                    if newest is None or log.timestamp > newest:
                        newest = log.timestamp
            
            return CacheStats(
                total_logs=len(self._cache),
                max_size=self._max_size,
                by_chain=by_chain,
                oldest_timestamp=oldest,
                newest_timestamp=newest,
            )
    
    def clear(self) -> int:
        """Clear all logs from the cache.
        
        Returns:
            Number of logs cleared
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._index.clear()
            return count
    
    def get_logs_by_hash(self, transaction_hash: str) -> List[Log]:
        """Get all logs for a specific transaction.
        
        Args:
            transaction_hash: Transaction hash to search for
        
        Returns:
            List of logs for the transaction
        """
        with self._lock:
            return [
                log for log in self._cache
                if log.transaction_hash.lower() == transaction_hash.lower()
            ]
