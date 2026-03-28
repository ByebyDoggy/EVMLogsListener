"""Tests for RPC exceptions."""

import pytest

from src.evm_chain_listener.rpc.exceptions import (
    RPCError,
    RateLimitError,
    LogLimitExceededError,
    ConnectionError,
    TimeoutError,
    InvalidResponseError,
    AllNodesFailedError,
    parse_rpc_error,
)


class TestRPCExceptions:
    """Test cases for RPC exceptions."""
    
    def test_rate_limit_error(self):
        """Test RateLimitError properties."""
        error = RateLimitError(
            message="Rate limit exceeded",
            node_url="https://example.com",
            retry_after=5,
        )
        
        assert error.code == 429
        assert error.node_url == "https://example.com"
        assert error.retry_after == 5
        assert "Rate limit exceeded" in str(error)
    
    def test_log_limit_exceeded_error(self):
        """Test LogLimitExceededError properties."""
        error = LogLimitExceededError(
            message="Log filter too large",
            node_url="https://example.com",
        )
        
        assert error.code == -32005
        assert error.node_url == "https://example.com"
    
    def test_connection_error(self):
        """Test ConnectionError properties."""
        error = ConnectionError(
            message="Connection refused",
            node_url="https://example.com",
        )
        
        assert error.node_url == "https://example.com"
        assert "Connection refused" in str(error)
    
    def test_timeout_error(self):
        """Test TimeoutError properties."""
        error = TimeoutError(
            message="Request timed out",
            node_url="https://example.com",
        )
        
        assert error.node_url == "https://example.com"
    
    def test_invalid_response_error(self):
        """Test InvalidResponseError properties."""
        error = InvalidResponseError(
            message="Invalid JSON response",
            node_url="https://example.com",
        )
        
        assert error.node_url == "https://example.com"
    
    def test_all_nodes_failed_error(self):
        """Test AllNodesFailedError properties."""
        error = AllNodesFailedError()
        
        assert error.node_url == ""
        assert "All RPC nodes failed" in str(error)
    
    def test_parse_rpc_error_rate_limit(self):
        """Test parsing rate limit error."""
        error_data = {"code": 429, "message": "Too many requests"}
        
        error = parse_rpc_error(error_data, "https://example.com")
        
        assert isinstance(error, RateLimitError)
        assert error.code == 429
    
    def test_parse_rpc_error_log_limit(self):
        """Test parsing log limit exceeded error."""
        error_data = {"code": -32005, "message": "Log filter too large"}
        
        error = parse_rpc_error(error_data, "https://example.com")
        
        assert isinstance(error, LogLimitExceededError)
        assert error.code == -32005
    
    def test_parse_rpc_error_generic(self):
        """Test parsing generic RPC error."""
        error_data = {"code": -32600, "message": "Invalid request"}
        
        error = parse_rpc_error(error_data, "https://example.com")
        
        assert isinstance(error, RPCError)
        assert error.code == -32600
