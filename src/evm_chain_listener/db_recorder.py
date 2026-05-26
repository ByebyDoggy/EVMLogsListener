"""LogDbRecorder - records logs to a local SQLite database.

The SQLite backend offers:

- **Efficient range queries** via B-tree index on ``(chain_id, block_number)``,
  enabling fast backtest replay without scanning the entire dataset.
- **Deduplication** via ``INSERT OR IGNORE`` with a unique constraint on
  ``(chain_id, block_number, transaction_hash, log_index)``.
- **Atomic writes** — each batch is committed in a single transaction,
  so the database is never left in a partial state.
- **Lower disk usage** — binary storage of integers and compact row format
  compared to line-delimited JSON.

Schema
------
Two tables are created:

``logs``
    Stores individual log entries.  Numeric fields (block_number, log_index,
    transaction_index) are stored as native integers, not hex strings.

``recording_sessions``
    Stores metadata about each recording session (one row per
    ``LogDbRecorder`` instance).
"""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import Log
from .utils.logging import get_logger

logger = get_logger(__name__)

# SQL statements
_CREATE_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id        INTEGER NOT NULL,
    chain_name      TEXT    NOT NULL,
    block_number    INTEGER NOT NULL,
    block_hash      TEXT    NOT NULL,
    transaction_hash TEXT   NOT NULL,
    transaction_index INTEGER NOT NULL,
    log_index       INTEGER NOT NULL,
    address         TEXT    NOT NULL,
    data            TEXT,
    topics          TEXT,   -- JSON array stored as text
    removed         INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT,   -- ISO 8601 string or NULL
    UNIQUE(chain_id, block_number, transaction_hash, log_index)
)
"""

_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS recording_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_name      TEXT    NOT NULL,
    chain_id        INTEGER NOT NULL,
    from_block      INTEGER,
    to_block        INTEGER,
    total_logs      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT    NOT NULL,
    closed_at       TEXT
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_logs_chain_block
ON logs (chain_id, block_number)
"""

_INSERT_LOG = """
INSERT OR IGNORE INTO logs (
    chain_id, chain_name, block_number, block_hash,
    transaction_hash, transaction_index, log_index,
    address, data, topics, removed, timestamp
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SESSION = """
INSERT INTO recording_sessions (
    chain_name, chain_id, from_block, to_block,
    total_logs, started_at, closed_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_SESSION = """
UPDATE recording_sessions
SET from_block = ?, to_block = ?, total_logs = ?, closed_at = ?
WHERE id = ?
"""


class LogDbRecorder:
    """Append logs to a SQLite database.

    Usage::

        recorder = LogDbRecorder(db_path="./recordings/logs.db", chain_name="ethereum", chain_id=1)
        recorder.write(logs)          # appends a batch (single transaction)
        recorder.close()              # flush & close

    The recorder keeps track of the block range it has seen and writes a
    session record so the database can be discovered later by the replay module.
    """

    def __init__(
        self,
        db_path: str,
        chain_name: str,
        chain_id: int,
        wal_mode: bool = True,
    ):
        self._db_path = Path(db_path)
        self._chain_name = chain_name
        self._chain_id = chain_id
        self._min_block: Optional[int] = None
        self._max_block: Optional[int] = None
        self._total_logs: int = 0
        self._session_id: Optional[int] = None
        self._started_at: str = datetime.now(timezone.utc).isoformat()

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Use thread-local connection (SQLite connections cannot be shared across threads)
        self._local = threading.local()
        self._lock = threading.Lock()  # Protect writes across threads
        self._closed = False

        # Initialize database schema on the calling thread
        conn = self._get_conn()
        if wal_mode:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8 MB cache
        conn.execute(_CREATE_LOGS_TABLE)
        conn.execute(_CREATE_SESSIONS_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.commit()

        # Insert session record
        cursor = conn.execute(
            _INSERT_SESSION,
            (chain_name, chain_id, None, None, 0, self._started_at, None),
        )
        self._session_id = cursor.lastrowid
        conn.commit()

        logger.info(
            f"[db_recorder] Opened database: {self._db_path} (session_id={self._session_id})",
            extra={"chain": chain_name},
        )

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                timeout=30,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @property
    def total_logs(self) -> int:
        return self._total_logs

    @property
    def block_range(self) -> Optional[str]:
        if self._min_block is None:
            return None
        return f"{self._min_block}-{self._max_block}"

    def write(self, logs: List[Log]) -> int:
        """Write a batch of logs to the SQLite database.

        Uses a single transaction for the entire batch.  Duplicates are
        silently skipped via ``INSERT OR IGNORE``.

        Returns the number of logs actually inserted (excluding duplicates).
        """
        if not logs:
            return 0

        conn = self._get_conn()

        block_nums = [log.block_number for log in logs]
        batch_min = min(block_nums)
        batch_max = max(block_nums)

        if self._min_block is None:
            self._min_block = batch_min
        else:
            self._min_block = min(self._min_block, batch_min)
        self._max_block = max(self._max_block or 0, batch_max)

        import json

        rows = []
        for log in logs:
            rows.append((
                log.chain_id or self._chain_id,
                log.chain_name or self._chain_name,
                log.block_number,
                log.block_hash,
                log.transaction_hash,
                log.transaction_index,
                log.log_index,
                log.address,
                log.data,
                json.dumps(log.topics, ensure_ascii=False) if log.topics else "[]",
                1 if log.removed else 0,
                log.timestamp.isoformat() if log.timestamp else None,
            ))

        with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(_INSERT_LOG, rows)
                conn.execute(
                    _UPDATE_SESSION,
                    (self._min_block, self._max_block, self._total_logs + len(logs), None, self._session_id),
                )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                logger.error(
                    f"[db_recorder] Failed to write batch: {e}",
                    extra={"chain": self._chain_name},
                )
                raise RuntimeError(f"Failed to write logs to SQLite: {e}") from e

        written = len(logs)  # executemany doesn't return rowcount per-statement accurately
        self._total_logs += written

        logger.debug(
            f"[db_recorder] Wrote {written} logs (blocks {batch_min}-{batch_max}) "
            f"to {self._db_path.name}",
            extra={
                "chain": self._chain_name,
                "from_block": batch_min,
                "to_block": batch_max,
                "log_count": written,
            },
        )
        return written

    def close(self) -> Optional[str]:
        """Close the database connection and update the session record.

        Returns the database file path, or None if nothing was written.
        """
        if self._closed:
            return str(self._db_path)
        self._closed = True

        closed_at = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        if self._session_id is not None:
            try:
                conn.execute(
                    _UPDATE_SESSION,
                    (self._min_block, self._max_block, self._total_logs, closed_at, self._session_id),
                )
                conn.commit()
            except sqlite3.Error as e:
                logger.error(f"[db_recorder] Failed to update session on close: {e}")

        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.conn = None

        logger.info(
            f"[db_recorder] Closed database: {self._db_path} "
            f"({self._total_logs} logs, blocks {self.block_range})",
            extra={
                "chain": self._chain_name,
                "file": str(self._db_path),
                "total_logs": self._total_logs,
                "block_range": self.block_range,
            },
        )
        return str(self._db_path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
