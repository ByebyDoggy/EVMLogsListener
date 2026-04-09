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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Log":
        """Create a Log from a serialized dict (to_dict format).

        Handles both hex-encoded (``to_dict``) and integer numeric fields
        (``to_push_dict``), as well as the ``_chain_id`` / ``_chain_name``
        metadata fields written by ``LogRecorder``.
        """
        def _int(val: Any) -> int:
            """Accept int or hex string."""
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.startswith("0x"):
                return int(val, 16)
            return int(val) if val is not None else 0

        ts = None
        ts_raw = data.get("timestamp")
        if ts_raw and isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return cls(
            address=data.get("address", ""),
            topics=data.get("topics", []),
            data=data.get("data", "0x"),
            block_number=_int(data.get("block_number", 0)),
            transaction_hash=data.get("transaction_hash", ""),
            log_index=_int(data.get("log_index", 0)),
            transaction_index=_int(data.get("transaction_index", 0)),
            block_hash=data.get("block_hash", ""),
            removed=data.get("removed", False),
            chain_id=data.get("chain_id") or data.get("_chain_id"),
            chain_name=data.get("chain_name") or data.get("_chain_name"),
            timestamp=ts,
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

    def to_push_dict(self) -> Dict[str, Any]:
        """Convert to dict for AlertProcessor ingestion (integer fields).

        Differs from to_dict(): block_number/log_index/transaction_index are
        plain integers (not hex strings), per the ingest spec.
        """
        return {
            "address": self.address,
            "topics": self.topics,
            "data": self.data,
            "block_number": self.block_number,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "transaction_index": self.transaction_index,
            "block_hash": self.block_hash,
            "removed": self.removed,
        }


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
    # apipool-ng based load balancing: list of raw RPC URLs.
    # When set, these URLs are used by EvmRpcPool (apipool-ng) instead of
    # the legacy RPCNodePool. Both can coexist; apipool_urls takes priority
    # when creating the EvmRpcPool instance.
    apipool_urls: Optional[List[str]] = None


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
class AlertProcessorConfig:
    """Configuration for the AlertProcessor push target."""
    enabled: bool = False
    url: str = ""
    push_interval_seconds: float = 5.0
    batch_size: int = 200
    max_payload_mb: float = 10.0
    retry_attempts: int = 3
    retry_base_delay_sec: float = 1.0
    timeout_seconds: float = 10.0
    reconnect_check_on_startup: bool = True
    replay_endpoint: str = "/ingest/logs/replay"


@dataclass
class PusherStats:
    """Runtime statistics for LogPusher."""
    total_pushed: int = 0
    total_failed: int = 0
    total_retried: int = 0
    last_push_at: Optional[datetime] = None
    last_push_log_count: int = 0
    buffer_size: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_pushed": self.total_pushed,
            "total_failed": self.total_failed,
            "total_retried": self.total_retried,
            "last_push_at": self.last_push_at.isoformat() if self.last_push_at else None,
            "last_push_log_count": self.last_push_log_count,
            "buffer_size": self.buffer_size,
            "last_error": self.last_error,
        }


@dataclass
class RecorderConfig:
    """Configuration for recording logs to local files."""
    enabled: bool = False
    # Directory to store JSONL recording files
    directory: str = "./recordings"


@dataclass
class ReplayConfig:
    """Configuration for replaying logs from local files."""
    # Path to a specific JSONL file to replay
    file_path: Optional[str] = None
    # OR: directory to scan for recordings (discovers .manifest.json files)
    directory: Optional[str] = None
    # Block range filter (applied to both file and directory mode)
    from_block: Optional[int] = None
    to_block: Optional[int] = None


@dataclass
class BacktestStartupConfig:
    """Configuration for auto-starting a backtest task on launch.

    When these fields are set, the backtest mode will automatically submit
    a task on startup instead of requiring a manual API call.
    """
    from_block: Optional[int] = None
    to_block: Optional[int] = None
    batch_size: int = 1000


@dataclass
class AppConfig:
    """Main application configuration."""
    chains: List[ChainConfig] = field(default_factory=list)
    cache: CacheConfig = field(default_factory=CacheConfig)
    api: APIConfig = field(default_factory=APIConfig)
    log: LogConfig = field(default_factory=LogConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    alert_processor: AlertProcessorConfig = field(default_factory=AlertProcessorConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    backtest: BacktestStartupConfig = field(default_factory=BacktestStartupConfig)


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


@dataclass
class BacktestConfig:
    """Configuration for backtest mode."""
    chain_name_or_id: str  # Chain name or chain_id to identify which chain to use
    from_block: int
    to_block: int
    batch_size: int = 1000  # Number of blocks per batch query


@dataclass
class BacktestResult:
    """Result of a backtest execution."""
    chain_name: str
    chain_id: int
    from_block: int
    to_block: int
    total_logs_found: int
    total_batches: int
    duration_seconds: float
    status: str  # "completed", "failed", "running"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_name": self.chain_name,
            "chain_id": self.chain_id,
            "from_block": self.from_block,
            "to_block": self.to_block,
            "total_logs_found": self.total_logs_found,
            "total_batches": self.total_batches,
            "duration_seconds": round(self.duration_seconds, 2),
            "status": self.status,
            "error": self.error,
        }


@dataclass
class BacktestTaskStatus:
    """Runtime status of a backtest task."""
    task_id: str
    chain_name: str
    chain_id: int
    from_block: int
    to_block: int
    status: str  # "pending", "running", "completed", "failed"
    progress_current_block: Optional[int] = None
    total_batches: int = 0
    completed_batches: int = 0
    logs_found: int = 0
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "chain_name": self.chain_name,
            "chain_id": self.chain_id,
            "from_block": self.from_block,
            "to_block": self.to_block,
            "status": self.status,
            "progress_current_block": self.progress_current_block,
            "total_batches": self.total_batches,
            "completed_batches": self.completed_batches,
            "logs_found": self.logs_found,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
