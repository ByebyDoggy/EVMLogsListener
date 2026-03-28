"""Tests for LogCache."""

import pytest
from datetime import datetime, timezone

from src.evm_chain_listener.cache.log_cache import LogCache
from src.evm_chain_listener.models import CacheConfig, Log


def create_log(
    tx_hash: str,
    log_idx: int,
    block_number: int,
    chain_id: int = 1,
) -> Log:
    """Helper to create a Log for testing."""
    return Log(
        address="0x1234567890123456789012345678901234567890",
        topics=["0x12345678"],
        data="0x",
        block_number=block_number,
        transaction_hash=tx_hash,
        log_index=log_idx,
        transaction_index=0,
        block_hash="0x" + "a" * 64,
        removed=False,
        chain_id=chain_id,
        chain_name="test",
        timestamp=datetime.now(timezone.utc),
    )


class TestLogCache:
    """Test cases for LogCache."""
    
    def test_add_single_log(self):
        """Test adding a single log."""
        cache = LogCache(CacheConfig(max_size=100))
        log = create_log("0xabc", 0, 100)
        
        result = cache.add(log)
        
        assert result is True
        assert cache.current_size == 1
        assert cache.total_added == 1
    
    def test_add_duplicate_log(self):
        """Test adding a duplicate log is rejected."""
        cache = LogCache(CacheConfig(max_size=100))
        log = create_log("0xabc", 0, 100)
        
        cache.add(log)
        result = cache.add(log)
        
        assert result is False
        assert cache.current_size == 1
        assert cache.total_added == 1
    
    def test_fifo_eviction(self):
        """Test that FIFO eviction works when cache is full."""
        cache = LogCache(CacheConfig(max_size=3))
        
        cache.add(create_log("0x1", 0, 100))
        cache.add(create_log("0x2", 0, 101))
        cache.add(create_log("0x3", 0, 102))
        
        assert cache.current_size == 3
        
        cache.add(create_log("0x4", 0, 103))
        
        assert cache.current_size == 3
        assert cache.total_dropped == 1
    
    def test_query_by_chain_id(self):
        """Test filtering logs by chain_id."""
        cache = LogCache(CacheConfig(max_size=100))
        
        cache.add(create_log("0x1", 0, 100, chain_id=1))
        cache.add(create_log("0x2", 0, 101, chain_id=1))
        cache.add(create_log("0x3", 0, 102, chain_id=56))
        
        result = cache.query(chain_id=1)
        
        assert result.total == 2
        assert all(log.chain_id == 1 for log in result.items)
    
    def test_query_by_block_range(self):
        """Test filtering logs by block range."""
        cache = LogCache(CacheConfig(max_size=100))
        
        cache.add(create_log("0x1", 0, 100))
        cache.add(create_log("0x2", 0, 101))
        cache.add(create_log("0x3", 0, 102))
        cache.add(create_log("0x4", 0, 103))
        
        result = cache.query(from_block=101, to_block=102)
        
        assert result.total == 2
        assert all(101 <= log.block_number <= 102 for log in result.items)
    
    def test_pagination(self):
        """Test pagination works correctly."""
        cache = LogCache(CacheConfig(max_size=100))
        
        for i in range(10):
            cache.add(create_log(f"0x{i}", 0, 100 + i))
        
        page1 = cache.query(page=1, page_size=3)
        page2 = cache.query(page=2, page_size=3)
        
        assert page1.total == 10
        assert page1.total_pages == 4
        assert len(page1.items) == 3
        
        assert page2.total == 10
        assert len(page2.items) == 3
    
    def test_get_stats(self):
        """Test cache statistics."""
        cache = LogCache(CacheConfig(max_size=100))
        
        cache.add(create_log("0x1", 0, 100, chain_id=1))
        cache.add(create_log("0x2", 0, 101, chain_id=1))
        cache.add(create_log("0x3", 0, 102, chain_id=56))
        
        stats = cache.get_stats()
        
        assert stats.total_logs == 3
        assert stats.max_size == 100
        assert stats.by_chain[1] == 2
        assert stats.by_chain[56] == 1
    
    def test_clear(self):
        """Test clearing the cache."""
        cache = LogCache(CacheConfig(max_size=100))
        
        cache.add(create_log("0x1", 0, 100))
        cache.add(create_log("0x2", 0, 101))
        
        count = cache.clear()
        
        assert count == 2
        assert cache.current_size == 0
    
    def test_max_size_enforcement(self):
        """Test that max size is strictly enforced."""
        cache = LogCache(CacheConfig(max_size=2))
        
        cache.add(create_log("0x1", 0, 100))
        cache.add(create_log("0x2", 0, 101))
        cache.add(create_log("0x3", 0, 102))
        
        assert cache.current_size == 2
        assert cache.total_dropped == 1
