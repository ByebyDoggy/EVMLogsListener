#!/usr/bin/env python3
"""Migrate JSONL recording files to SQLite database.

Usage:
    python scripts/migrate_jsonl_to_sqlite.py ./recordings [--output ./recordings/logs.db] [--batch-size 5000]

This script reads all .jsonl files in the specified directory (or a single
file) and imports them into a SQLite database compatible with
``LogDbRecorder`` / ``LogDbReplaySource``.

Features:
- Streaming reads: processes one line at a time to keep memory usage low.
- Batch inserts: commits every ``--batch-size`` rows for good write performance.
- Deduplication: uses ``INSERT OR IGNORE`` matching the recorder's unique
  constraint on ``(chain_id, block_number, transaction_hash, log_index)``.
- Session tracking: creates one ``recording_sessions`` entry per input file.
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Reuse the same SQL as LogDbRecorder
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
    topics          TEXT,
    removed         INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT,
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


def _int_or_hex(val) -> int:
    """Convert a value to int, handling hex strings like '0x1a'."""
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.startswith("0x"):
        return int(val, 16)
    return int(val) if val is not None else 0


def migrate_file(jsonl_path: Path, db_path: Path, batch_size: int = 5000) -> int:
    """Migrate a single JSONL file to the SQLite database.

    Returns the number of logs imported.
    """
    print(f"  Reading: {jsonl_path.name} ...", end=" ", flush=True)
    start = time.time()
    total_imported = 0
    min_block: Optional[int] = None
    max_block: Optional[int] = None
    chain_id = 0
    chain_name = "unknown"

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_rows = []
        line_no = 0

        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line_no += 1

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"\n    WARNING: Skipping malformed line {line_no}: {e}")
                    continue

                # Extract metadata
                cid = record.get("chain_id") or record.get("_chain_id") or 0
                cname = record.get("chain_name") or record.get("_chain_name") or "unknown"
                if chain_id == 0:
                    chain_id = cid
                    chain_name = cname

                block_num = _int_or_hex(record.get("block_number", 0))

                if min_block is None or block_num < min_block:
                    min_block = block_num
                if max_block is None or block_num > max_block:
                    max_block = block_num

                topics = record.get("topics", [])
                ts = record.get("timestamp")

                batch_rows.append((
                    _int_or_hex(cid),
                    cname,
                    block_num,
                    record.get("block_hash", ""),
                    record.get("transaction_hash", ""),
                    _int_or_hex(record.get("transaction_index", 0)),
                    _int_or_hex(record.get("log_index", 0)),
                    record.get("address", ""),
                    record.get("data", "0x"),
                    json.dumps(topics, ensure_ascii=False) if topics else "[]",
                    1 if record.get("removed", False) else 0,
                    ts,
                ))

                if len(batch_rows) >= batch_size:
                    conn.executemany(_INSERT_LOG, batch_rows)
                    conn.commit()
                    conn.execute("BEGIN IMMEDIATE")
                    total_imported += len(batch_rows)
                    batch_rows = []

        # Flush remaining
        if batch_rows:
            conn.executemany(_INSERT_LOG, batch_rows)
            total_imported += len(batch_rows)

        # Insert session record
        started_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            _INSERT_SESSION,
            (chain_name, chain_id, min_block, max_block, total_imported, started_at, started_at),
        )
        conn.commit()

        elapsed = time.time() - start
        print(f"done ({total_imported} logs, blocks {min_block}-{max_block}, {elapsed:.1f}s)")
    except Exception as e:
        conn.rollback()
        print(f"FAILED: {e}")
        raise
    finally:
        conn.close()

    return total_imported


def main():
    parser = argparse.ArgumentParser(
        description="Migrate JSONL recording files to SQLite database"
    )
    parser.add_argument(
        "input",
        help="Path to a JSONL file or directory containing JSONL files",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output SQLite database path (default: <input_dir>/logs.db)",
    )
    parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=5000,
        help="Number of rows per batch insert (default: 5000)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)

    # Determine files to migrate
    if input_path.is_file():
        jsonl_files = [input_path]
        base_dir = input_path.parent
    elif input_path.is_dir():
        jsonl_files = sorted(input_path.glob("*.jsonl"))
        base_dir = input_path
    else:
        print(f"Error: {input_path} does not exist", file=sys.stderr)
        sys.exit(1)

    if not jsonl_files:
        print("No JSONL files found to migrate.", file=sys.stderr)
        sys.exit(0)

    # Determine output path
    db_path = Path(args.output) if args.output else base_dir / "logs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize database schema
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_CREATE_LOGS_TABLE)
    conn.execute(_CREATE_SESSIONS_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.commit()
    conn.close()

    print(f"Migrating {len(jsonl_files)} JSONL file(s) to {db_path}")
    print("=" * 60)

    grand_total = 0
    for jsonl_file in jsonl_files:
        total = migrate_file(jsonl_file, db_path, args.batch_size)
        grand_total += total

    print("=" * 60)
    print(f"Migration complete: {grand_total} total logs imported to {db_path}")

    # Print database size
    db_size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"Database size: {db_size_mb:.1f} MB")

    # Compare with JSONL total size
    jsonl_total_mb = sum(f.stat().st_size for f in jsonl_files) / (1024 * 1024)
    print(f"JSONL files total: {jsonl_total_mb:.1f} MB")
    if jsonl_total_mb > 0:
        print(f"Size ratio: {db_size_mb / jsonl_total_mb:.1%} of original")


if __name__ == "__main__":
    main()
