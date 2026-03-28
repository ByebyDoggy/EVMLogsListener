"""Data models for EVM Chain Listener."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Log:
    """Represents a blockchain log entry."""
    address: str
    topics: List[str]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int
    transaction_index: int
    block_hash: str
    removed: bool = False
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    timestamp: Optional[datetime] = None

    @classmethod
    def from_rpc_response(cls, data: Dict[str, Any], chain_id: int = 0, chain_name: str = "") -> "Log":
        """Create a Log from RPC response data."""
        return cls(
            address=data.get("address", ""),
            topics=data.get("topics", []),
            data=data.get("data", "0x"),
            block_number=int(data.get("blockNumber", "0x0"), 16),
            transaction_hash=data.get("transactionHash", ""),
            log_index=int(data.get("logIndex", "0x0"), 16),
            transaction_index=int(data.get("transactionIndex", "0x0"), 16),
            block_hash=data.get("blockHash", ""),
            removed=data.get("removed", False),
            chain_id=chain_id,
            chain_name=chain_name,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "address": self.address,
            "topics": self.topics,
            "data": self.data,
            "block_number": hex(self.block_number) if isinstance(self.block_number, int) else self.block_number,
            "transaction_hash": self.transaction_hash,
            "log_index": hex(self.log_index) if isinstance(self.log_index, int) else self.log_index,
            "transaction_index": hex(self.transaction_index) if isinstance(self.transaction_index, int) else self.transaction_index,
            "block_hash": self.block_hash,
            "removed": self.removed,
            "chain_id": self.chain_id,
            "chain_name": self.chain_name,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }

    @property
    def unique_key(self) -> str:
        """Generate unique key for deduplication."""
        return f"{self.transaction_hash}:{self.log_index}"


@dataclass
class RateLimit:
    """Rate limit configuration for RPC node."""
    requests_per_second: int = 10
    burst: int = 20


@dataclass
class RPCNodeConfig:
    """Configuration for an RPC node."""
    url: str
    priority: int = 100
    node_type: str = "public"
    rate_limit: Optional[RateLimit] = None


@dataclass
class ChainConfig:
    """Configuration for a blockchain chain listener."""
    name: str
    chain_id: int
    poll_interval: int = 30
    rpc_nodes: List[RPCNodeConfig] = field(default_factory=list)
    address_filter: Optional[List[str]] = None
    topics_filter: Optional[List[str]] = None


@dataclass
class CacheConfig:
    """Configuration for log cache."""
    max_size: int = 10000


@dataclass
class APIConfig:
    """Configuration for API server."""
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class LogConfig:
    """Configuration for logging."""
    level: str = "INFO"
    format: str = "json"
    output: str = "stdout"
    file_path: Optional[str] = None


@dataclass
class HealthConfig:
    """Configuration for health check."""
    enabled: bool = True
    endpoint: str = "/health"


@dataclass
class AppConfig:
    """Main application configuration."""
    chains: List[ChainConfig] = field(default_factory=list)
    cache: CacheConfig = field(default_factory=CacheConfig)
    api: APIConfig = field(default_factory=APIConfig)
    log: LogConfig = field(default_factory=LogConfig)
    health: HealthConfig = field(default_factory=HealthConfig)


@dataclass
class PaginatedResult:
    """Paginated query result."""
    items: List[Log]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        """Calculate total pages."""
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "logs": [log.to_dict() for log in self.items],
            "pagination": {
                "page": self.page,
                "page_size": self.page_size,
                "total": self.total,
                "total_pages": self.total_pages,
            }
        }


@dataclass
class CacheStats:
    """Cache statistics."""
    total_logs: int
    max_size: int
    by_chain: Dict[int, int]
    oldest_timestamp: Optional[datetime]
    newest_timestamp: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_logs": self.total_logs,
            "max_size": self.max_size,
            "by_chain": {str(k): v for k, v in self.by_chain.items()},
            "oldest_log_timestamp": self.oldest_timestamp.isoformat() if self.oldest_timestamp else None,
            "newest_log_timestamp": self.newest_timestamp.isoformat() if self.newest_timestamp else None,
        }


@dataclass
class ChainStatus:
    """Status of a chain listener."""
    name: str
    chain_id: int
    status: str = "stopped"
    last_block: Optional[int] = None
    last_poll: Optional[datetime] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "chain_id": self.chain_id,
            "status": self.status,
            "last_block": self.last_block,
            "last_poll": self.last_poll.isoformat() if self.last_poll else None,
            "error": self.error,
        }


@dataclass
class NodeHealthStatus:
    """Health status of RPC nodes."""
    active: str
    available: List[str]
    failed: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "active": self.active,
            "available": self.available,
            "failed": self.failed,
        }
