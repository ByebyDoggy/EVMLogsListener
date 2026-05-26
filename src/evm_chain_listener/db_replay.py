"""LogDbReplaySource - replays logs from a SQLite database.

Reads logs recorded by :class:`LogDbRecorder` and feeds them through the
same callback path as live RPC queries, ensuring data structure parity.

Key design decisions:
- Uses indexed ``WHERE block_number BETWEEN ? AND ?`` for efficient range
  queries — no full-table scan regardless of dataset size.
- Supports block-level batching with configurable interval between batches,
  allowing downstream consumers to process logs at a controlled pace.
- Row-to-Log conversion via ``_row_to_log()`` produces identical ``Log`` objects
  as those from live RPC queries.
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import Log
from .utils.logging import get_logger

logger = get_logger(__name__)

_QUERY_LOGS_SQL = """
SELECT
    chain_id, chain_name, block_number, block_hash,
    transaction_hash, transaction_index, log_index,
    address, data, topics, removed, timestamp
FROM logs
WHERE chain_id = ?
  AND block_number >= ?
  AND block_number <= ?
ORDER BY block_number ASC, log_index ASC
"""

_QUERY_LOGS_NO_BLOCK_SQL = """
SELECT
    chain_id, chain_name, block_number, block_hash,
    transaction_hash, transaction_index, log_index,
    address, data, topics, removed, timestamp
FROM logs
WHERE chain_id = ?
ORDER BY block_number ASC, log_index ASC
"""

_COUNT_LOGS_SQL = """
SELECT COUNT(*) FROM logs
WHERE chain_id = ?
  AND block_number >= ?
  AND block_number <= ?
"""

_QUERY_SESSIONS_SQL = """
SELECT chain_name, chain_id, from_block, to_block,
       total_logs, started_at, closed_at
FROM recording_sessions
ORDER BY id ASC
"""


class LogDbReplaySource:
    """Replays logs from a SQLite database recording.

    This class reads a database produced by :class:`LogDbRecorder` and calls
    ``log_callback`` for each batch of logs found, mimicking the behaviour
    of :class:`BacktestRunner` (RPC).

    Logs are replayed in block-level batches: each batch contains all logs
    from ``blocks_per_batch`` consecutive blocks. After each batch is
    delivered via ``log_callback``, the replay pauses for
    ``batch_interval_seconds`` before proceeding to the next batch.

    Usage::

        source = LogDbReplaySource(
            db_path="./recordings/logs.db",
            chain_name="ethereum",
            chain_id=1,
            log_callback=_on_logs_received,
            blocks_per_batch=2,
            batch_interval_seconds=5.0,
        )
        total = await source.replay(from_block=100, to_block=200)
    """

    def __init__(
        self,
        db_path: str,
        chain_name: str,
        chain_id: int,
        log_callback: Callable[[List[Log]], None],
        blocks_per_batch: int = 2,
        batch_interval_seconds: float = 5.0,
    ):
        self._db_path = Path(db_path)
        self._chain_name = chain_name
        self._chain_id = chain_id
        self._log_callback = log_callback
        self._blocks_per_batch = max(1, blocks_per_batch)
        self._batch_interval_seconds = max(0.0, batch_interval_seconds)

    @staticmethod
    def discover_sessions(db_path: str) -> List[Dict]:
        """Discover all recording sessions in a SQLite database.

        Returns a list of session dicts sorted by ``started_at`` ascending.
        """
        if not Path(db_path).exists():
            return []

        sessions = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                for row in conn.execute(_QUERY_SESSIONS_SQL):
                    sessions.append({
                        "chain_name": row["chain_name"],
                        "chain_id": row["chain_id"],
                        "from_block": row["from_block"],
                        "to_block": row["to_block"],
                        "total_logs": row["total_logs"],
                        "started_at": row["started_at"],
                        "closed_at": row["closed_at"],
                    })
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[db_replay] Failed to read sessions from {db_path}: {e}")

        return sessions

    @staticmethod
    def _row_to_log(row: sqlite3.Row) -> Log:
        """Convert a database row to a Log object."""
        topics = []
        if row["topics"]:
            try:
                topics = json.loads(row["topics"])
            except (json.JSONDecodeError, TypeError):
                topics = []

        ts = None
        if row["timestamp"]:
            try:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return Log(
            address=row["address"],
            topics=topics,
            data=row["data"] or "0x",
            block_number=row["block_number"],
            transaction_hash=row["transaction_hash"],
            log_index=row["log_index"],
            transaction_index=row["transaction_index"],
            block_hash=row["block_hash"],
            removed=bool(row["removed"]),
            chain_id=row["chain_id"],
            chain_name=row["chain_name"],
            timestamp=ts,
        )

    async def replay(
        self,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
    ) -> int:
        """Replay logs from the SQLite database.

        Logs are delivered in block-level batches: each batch contains all
        logs from ``blocks_per_batch`` consecutive blocks.  After each
        batch is delivered via ``log_callback``, the replay pauses for
        ``batch_interval_seconds`` before proceeding to the next batch.

        Args:
            from_block: Only replay logs with block_number >= this value.
            to_block: Only replay logs with block_number <= this value.

        Returns:
            Total number of logs replayed.
        """
        if not self._db_path.exists():
            raise FileNotFoundError(f"Database file not found: {self._db_path}")

        logger.info(
            f"[db_replay] Starting replay from {self._db_path.name}",
            extra={
                "chain": self._chain_name,
                "file": str(self._db_path),
                "from_block": from_block,
                "to_block": to_block,
                "blocks_per_batch": self._blocks_per_batch,
                "batch_interval_seconds": self._batch_interval_seconds,
            },
        )

        total_replayed = 0
        conn: Optional[sqlite3.Connection] = None

        try:
            # Open in read-only mode via URI
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True,
                timeout=30,
            )
            conn.row_factory = sqlite3.Row

            # Determine the actual block range to replay
            if from_block is None or to_block is None:
                # Auto-detect from the database
                range_sql = "SELECT MIN(block_number), MAX(block_number) FROM logs WHERE chain_id = ?"
                row = conn.execute(range_sql, (self._chain_id,)).fetchone()
                actual_from = from_block if from_block is not None else (row[0] if row and row[0] is not None else 0)
                actual_to = to_block if to_block is not None else (row[1] if row and row[1] is not None else 0)
            else:
                actual_from = from_block
                actual_to = to_block

            # Log total matching count for progress info
            if from_block is not None and to_block is not None:
                count_row = conn.execute(
                    _COUNT_LOGS_SQL,
                    (self._chain_id, from_block, to_block),
                ).fetchone()
                total_matching = count_row[0] if count_row else 0
                logger.info(
                    f"[db_replay] Found {total_matching} logs in range "
                    f"[{from_block}, {to_block}] for chain_id={self._chain_id}",
                    extra={"chain": self._chain_name},
                )

            # Replay in block-level batches
            batch_num = 0
            batch_start = actual_from
            while batch_start <= actual_to:
                batch_end = min(batch_start + self._blocks_per_batch - 1, actual_to)

                # Fetch all logs for this block batch
                cursor = conn.execute(
                    _QUERY_LOGS_SQL,
                    (self._chain_id, batch_start, batch_end),
                )

                batch: List[Log] = []
                for row in cursor:
                    log = self._row_to_log(row)
                    # Ensure chain metadata is set
                    if log.chain_id is None:
                        log.chain_id = self._chain_id
                    if log.chain_name is None:
                        log.chain_name = self._chain_name
                    batch.append(log)

                if batch:
                    self._log_callback(batch)
                    total_replayed += len(batch)
                    batch_num += 1
                    logger.debug(
                        f"[db_replay] Replayed batch {batch_num}: {len(batch)} logs "
                        f"from blocks [{batch_start}, {batch_end}] "
                        f"(total so far: {total_replayed})",
                        extra={"chain": self._chain_name},
                    )

                # Wait between batches (skip after the last batch)
                if batch_end < actual_to and self._batch_interval_seconds > 0:
                    await asyncio.sleep(self._batch_interval_seconds)

                batch_start = batch_end + 1

        except sqlite3.Error as e:
            logger.error(
                f"[db_replay] Database error during replay: {e}",
                extra={"chain": self._chain_name},
            )
            raise RuntimeError(f"SQLite replay failed: {e}") from e
        finally:
            if conn:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

        logger.info(
            f"[db_replay] Completed: {total_replayed} logs replayed from "
            f"{self._db_path.name}",
            extra={
                "chain": self._chain_name,
                "total_logs": total_replayed,
            },
        )
        return total_replayed
