"""Tests for data models."""

import pytest
from datetime import datetime, timezone

from src.evm_chain_listener.models import (
    Log,
    PaginatedResult,
    CacheStats,
    ChainStatus,
)


class TestLog:
    """Test cases for Log model."""
    
    def test_from_rpc_response(self):
        """Test creating Log from RPC response."""
        rpc_data = {
            "address": "0x1234567890123456789012345678901234567890",
            "topics": ["0x12345678", "0xaabbccdd"],
            "data": "0x1234",
            "blockNumber": "0x100",
            "transactionHash": "0xabcdef",
            "logIndex": "0x1",
            "transactionIndex": "0x0",
            "blockHash": "0xdef123",
            "removed": False,
        }
        
        log = Log.from_rpc_response(rpc_data, chain_id=1, chain_name="ethereum")
        
        assert log.address == "0x1234567890123456789012345678901234567890"
        assert log.topics == ["0x12345678", "0xaabbccdd"]
        assert log.data == "0x1234"
        assert log.block_number == 256
        assert log.transaction_hash == "0xabcdef"
        assert log.log_index == 1
        assert log.transaction_index == 0
        assert log.block_hash == "0xdef123"
        assert log.removed is False
        assert log.chain_id == 1
        assert log.chain_name == "ethereum"
    
    def test_unique_key(self):
        """Test unique key generation."""
        log = Log(
            address="0x1234",
            topics=[],
            data="0x",
            block_number=100,
            transaction_hash="0xabc",
            log_index=5,
            transaction_index=0,
            block_hash="0xdef",
        )
        
        assert log.unique_key == "0xabc:5"
    
    def test_to_dict(self):
        """Test converting Log to dictionary."""
        log = Log(
            address="0x1234",
            topics=["0x1234"],
            data="0x",
            block_number=100,
            transaction_hash="0xabc",
            log_index=5,
            transaction_index=0,
            block_hash="0xdef",
            removed=False,
            chain_id=1,
            chain_name="ethereum",
            timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        )
        
        result = log.to_dict()
        
        assert result["address"] == "0x1234"
        assert result["topics"] == ["0x1234"]
        assert result["block_number"] == "0x64"
        assert result["chain_id"] == 1
        assert result["chain_name"] == "ethereum"


class TestPaginatedResult:
    """Test cases for PaginatedResult model."""
    
    def test_total_pages_calculation(self):
        """Test total pages calculation."""
        result = PaginatedResult(
            items=[],
            total=100,
            page=1,
            page_size=10,
        )
        
        assert result.total_pages == 10
    
    def test_total_pages_with_remainder(self):
        """Test total pages with remainder."""
        result = PaginatedResult(
            items=[],
            total=95,
            page=1,
            page_size=10,
        )
        
        assert result.total_pages == 10
    
    def test_total_pages_zero(self):
        """Test total pages when empty."""
        result = PaginatedResult(
            items=[],
            total=0,
            page=1,
            page_size=10,
        )
        
        assert result.total_pages == 0
    
    def test_to_dict(self):
        """Test converting to dictionary."""
        result = PaginatedResult(
            items=[],
            total=50,
            page=2,
            page_size=10,
        )
        
        data = result.to_dict()
        
        assert data["pagination"]["page"] == 2
        assert data["pagination"]["page_size"] == 10
        assert data["pagination"]["total"] == 50
        assert data["pagination"]["total_pages"] == 5


class TestCacheStats:
    """Test cases for CacheStats model."""
    
    def test_to_dict(self):
        """Test converting to dictionary."""
        stats = CacheStats(
            total_logs=100,
            max_size=1000,
            by_chain={1: 60, 56: 40},
            oldest_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            newest_timestamp=datetime(2024, 1, 15, tzinfo=timezone.utc),
        )
        
        result = stats.to_dict()
        
        assert result["total_logs"] == 100
        assert result["max_size"] == 1000
        assert result["by_chain"] == {"1": 60, "56": 40}


class TestChainStatus:
    """Test cases for ChainStatus model."""
    
    def test_to_dict(self):
        """Test converting to dictionary."""
        status = ChainStatus(
            name="ethereum",
            chain_id=1,
            status="running",
            last_block=19000000,
            last_poll=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        )
        
        result = status.to_dict()
        
        assert result["name"] == "ethereum"
        assert result["chain_id"] == 1
        assert result["status"] == "running"
        assert result["last_block"] == 19000000
