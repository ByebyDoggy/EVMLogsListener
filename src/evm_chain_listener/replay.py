"""LogReplaySource - replays logs from local JSONL files.

Reads logs recorded by ``LogRecorder`` and feeds them through the same
callback path as live RPC queries, ensuring data structure parity.

Key design decisions:
- The JSONL format stores logs using ``Log.to_dict()`` (hex-encoded numbers),
  so ``Log.from_dict()`` handles the hex→int conversion.
- Filtering by block range and chain is supported.
- Logs are yielded in batches to simulate the real-time polling experience.
"""

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import Log
from .utils.logging import get_logger

logger = get_logger(__name__)


class LogReplaySource:
    """Replays logs from a local JSONL recording file.

    This class reads a recording file produced by ``LogRecorder`` and calls
    ``log_callback`` for each batch of logs found, mimicking the behaviour
    of ``ChainListener._poll()`` in realtime mode or ``BacktestRunner`` in
    backtest mode.

    Usage::

        source = LogReplaySource(
            file_path="./recordings/ethereum_20260409_120000.jsonl",
            chain_name="ethereum",
            chain_id=1,
            log_callback=_on_logs_received,
            batch_size=1000,
        )
        total = await source.replay(from_block=100, to_block=200)
    """

    def __init__(
        self,
        file_path: str,
        chain_name: str,
        chain_id: int,
        log_callback: Callable[[List[Log]], None],
        batch_size: int = 1000,
    ):
        self._file_path = Path(file_path)
        self._chain_name = chain_name
        self._chain_id = chain_id
        self._log_callback = log_callback
        self._batch_size = batch_size

    @staticmethod
    def discover_recordings(
        directory: str,
        chain_name: Optional[str] = None,
    ) -> List[Dict]:
        """Discover all recording files in a directory.

        Returns a list of manifest dicts (or best-effort metadata if no
        manifest exists), sorted by ``from_block`` ascending.
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            return []

        results = []
        for manifest_file in sorted(dir_path.glob("*.manifest.json")):
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                manifest["manifest_path"] = str(manifest_file)
                if chain_name and manifest.get("chain_name") != chain_name:
                    continue
                results.append(manifest)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[replay] Failed to read manifest {manifest_file}: {e}")

        # Fallback: discover JSONL files without manifests
        manifest_stems = {m.get("file", "") for m in results}
        for jsonl_file in sorted(dir_path.glob("*.jsonl")):
            if jsonl_file.name in manifest_stems:
                continue
            # Try to infer metadata from filename
            results.append({
                "file": jsonl_file.name,
                "manifest_path": None,
                "chain_name": chain_name or "unknown",
                "chain_id": 0,
                "from_block": None,
                "to_block": None,
                "total_logs": None,
            })

        return results

    async def replay(
        self,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
    ) -> int:
        """Replay logs from the recording file.

        Args:
            from_block: Only replay logs with block_number >= this value.
            to_block: Only replay logs with block_number <= this value.

        Returns:
            Total number of logs replayed.
        """
        if not self._file_path.exists():
            raise FileNotFoundError(f"Recording file not found: {self._file_path}")

        logger.info(
            f"[replay] Starting replay from {self._file_path.name}",
            extra={
                "chain": self._chain_name,
                "file": str(self._file_path),
                "from_block": from_block,
                "to_block": to_block,
            },
        )

        total_replayed = 0
        batch: List[Log] = []

        with open(self._file_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[replay] Skipping malformed line {line_no}: {e}",
                    )
                    continue

                log = Log.from_dict(record)

                # Apply block range filter
                if from_block is not None and log.block_number < from_block:
                    continue
                if to_block is not None and log.block_number > to_block:
                    continue

                # Ensure chain metadata is set
                if log.chain_id is None:
                    log.chain_id = self._chain_id
                if log.chain_name is None:
                    log.chain_name = self._chain_name

                batch.append(log)

                if len(batch) >= self._batch_size:
                    self._log_callback(batch)
                    total_replayed += len(batch)
                    logger.info(
                        f"[replay] Replayed batch: {len(batch)} logs "
                        f"(total so far: {total_replayed})",
                        extra={"chain": self._chain_name},
                    )
                    batch = []

        # Flush remaining
        if batch:
            self._log_callback(batch)
            total_replayed += len(batch)

        logger.info(
            f"[replay] Completed: {total_replayed} logs replayed from "
            f"{self._file_path.name}",
            extra={
                "chain": self._chain_name,
                "total_logs": total_replayed,
            },
        )
        return total_replayed
