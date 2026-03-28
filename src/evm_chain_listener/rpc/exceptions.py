"""RPC exceptions for EVM Chain Listener."""

from typing import Optional


class RPCError(Exception):
    """Base exception for RPC errors."""
    
    def __init__(self, message: str, code: Optional[int] = None, node_url: str = ""):
        super().__init__(message)
        self.code = code
        self.node_url = node_url


class RateLimitError(RPCError):
    """Exception raised when RPC node rate limit is exceeded."""
    
    def __init__(self, message: str = "Rate limit exceeded", node_url: str = "", retry_after: Optional[int] = None):
        super().__init__(message, code=429, node_url=node_url)
        self.retry_after = retry_after


class LogLimitExceededError(RPCError):
    """Exception raised when log filter result is too large.
    
    This error code is typically -32005 in Ethereum RPC errors.
    """
    
    def __init__(self, message: str = "Log filter too large", node_url: str = ""):
        super().__init__(message, code=-32005, node_url=node_url)


class ConnectionError(RPCError):
    """Exception raised when connection to RPC node fails."""
    
    def __init__(self, message: str = "Connection failed", node_url: str = ""):
        super().__init__(message, code=None, node_url=node_url)


class TimeoutError(RPCError):
    """Exception raised when RPC request times out."""
    
    def __init__(self, message: str = "Request timed out", node_url: str = ""):
        super().__init__(message, code=None, node_url=node_url)


class InvalidResponseError(RPCError):
    """Exception raised when RPC response is invalid."""
    
    def __init__(self, message: str = "Invalid response", node_url: str = ""):
        super().__init__(message, code=None, node_url=node_url)


class AllNodesFailedError(RPCError):
    """Exception raised when all RPC nodes fail."""
    
    def __init__(self, message: str = "All RPC nodes failed"):
        super().__init__(message, code=None, node_url="")


def parse_rpc_error(error_data: dict, node_url: str = "") -> RPCError:
    """Parse RPC error response into appropriate exception.
    
    Args:
        error_data: Error data from RPC response
        node_url: URL of the RPC node that returned the error
    
    Returns:
        Appropriate RPCError subclass
    """
    code = error_data.get("code")
    message = error_data.get("message", "Unknown error")
    
    if code == 429:
        retry_after = error_data.get("retryAfter")
        return RateLimitError(message=message, node_url=node_url, retry_after=retry_after)
    
    if code == -32005:
        return LogLimitExceededError(message=message, node_url=node_url)
    
    return RPCError(message=message, code=code, node_url=node_url)
