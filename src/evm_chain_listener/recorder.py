"""LogRecorder - records logs to local JSONL files for later replay.

Each log entry is stored using ``Log.to_dict()`` format (hex-encoded numeric
fields) which is the same representation returned by the local API.  This
ensures the recorded file can be read back by ``LogReplaySource`` and converted
to ``Log`` objects that are identical to those produced by live RPC queries.

File naming convention:
    <dir>/<chain_name>_<from_block>-<to_block>.jsonl

If the directory does not exist it is created automatically.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import Log
from .utils.logging import get_logger

logger = get_logger(__name__)


class LogRecorder:
    """Append logs to a JSONL file on disk.

    Usage::

        recorder = LogRecorder(directory="./recordings", chain_name="ethereum")
        recorder.write(logs)          # appends a batch
        recorder.close()              # flush & close

    The recorder keeps track of the block range it has seen and writes a
    manifest entry so the file can be discovered later by the replay module.
    """

    def __init__(
        self,
        directory: str,
        chain_name: str,
        chain_id: int,
    ):
        self._directory = Path(directory)
        self._chain_name = chain_name
        self._chain_id = chain_id
        self._min_block: Optional[int] = None
        self._max_block: Optional[int] = None
        self._total_logs: int = 0
        self._file = None
        self._file_path: Optional[Path] = None

        # Ensure directory exists
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def total_logs(self) -> int:
        return self._total_logs

    @property
    def block_range(self) -> Optional[str]:
        if self._min_block is None:
            return None
        return f"{self._min_block}-{self._max_block}"

    def _ensure_file_open(self) -> None:
        """Lazy-open the file on first write."""
        if self._file is not None:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{self._chain_name}_{timestamp}.jsonl"
        self._file_path = self._directory / filename
        self._file = open(self._file_path, "a", encoding="utf-8")

        logger.info(
            f"[recorder] Opened recording file: {self._file_path}",
            extra={"chain": self._chain_name},
        )

    def write(self, logs: List[Log]) -> int:
        """Write a batch of logs to the JSONL file.

        Returns the number of logs written.
        """
        if not logs:
            return 0

        self._ensure_file_open()

        block_nums = [l.block_number for l in logs]
        batch_min = min(block_nums)
        batch_max = max(block_nums)

        if self._min_block is None:
            self._min_block = batch_min
        else:
            self._min_block = min(self._min_block, batch_min)
        self._max_block = max(self._max_block or 0, batch_max)

        written = 0
        for log in logs:
            record = log.to_dict()
            # Append metadata for replay identification
            record["_chain_id"] = self._chain_id
            record["_chain_name"] = self._chain_name
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

        self._total_logs += written
        self._file.flush()
        os.fsync(self._file.fileno())

        logger.debug(
            f"[recorder] Wrote {written} logs (blocks {batch_min}-{batch_max}) "
            f"to {self._file_path.name}",
            extra={
                "chain": self._chain_name,
                "from_block": batch_min,
                "to_block": batch_max,
                "log_count": written,
            },
        )
        return written

    def close(self) -> Optional[str]:
        """Flush, close the file and write a manifest.

        Returns the file path of the recording, or None if nothing was written.
        """
        if self._file is None:
            return None

        self._file.flush()
        self._file.close()
        self._file = None

        # Write manifest for easy discovery by replay
        if self._min_block is not None:
            self._write_manifest()

        logger.info(
            f"[recorder] Closed recording: {self._file_path} "
            f"({self._total_logs} logs, blocks {self.block_range})",
            extra={
                "chain": self._chain_name,
                "file": str(self._file_path),
                "total_logs": self._total_logs,
                "block_range": self.block_range,
            },
        )
        return str(self._file_path)

    def _write_manifest(self) -> None:
        """Write a small manifest file alongside the JSONL file."""
        if not self._file_path:
            return
        manifest_path = self._file_path.with_suffix(".manifest.json")
        manifest = {
            "chain_name": self._chain_name,
            "chain_id": self._chain_id,
            "from_block": self._min_block,
            "to_block": self._max_block,
            "total_logs": self._total_logs,
            "file": self._file_path.name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
