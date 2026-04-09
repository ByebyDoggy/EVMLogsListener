"""Tests for LogRecorder and LogReplaySource."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from evm_chain_listener.models import Log
from evm_chain_listener.recorder import LogRecorder
from evm_chain_listener.replay import LogReplaySource


# ---------- helpers ----------

def _make_log(
    block_number: int = 100,
    log_index: int = 0,
    tx_hash: str = "",
    chain_id: int = 1,
    chain_name: str = "ethereum",
) -> Log:
    return Log(
        address="0xDeadBeef",
        topics=["0xtopic1"],
        data="0xdata",
        block_number=block_number,
        transaction_hash=tx_hash or f"0xtx_{block_number}_{log_index}",
        log_index=log_index,
        transaction_index=0,
        block_hash=f"0xblock_{block_number}",
        removed=False,
        chain_id=chain_id,
        chain_name=chain_name,
    )


# ---------- Log.from_dict ----------

class TestLogFromDict:
    def test_roundtrip_to_dict_from_dict(self):
        log = _make_log()
        d = log.to_dict()
        restored = Log.from_dict(d)
        assert restored.address == log.address
        assert restored.block_number == log.block_number
        assert restored.log_index == log.log_index
        assert restored.transaction_hash == log.transaction_hash
        assert restored.chain_id == log.chain_id
        assert restored.chain_name == log.chain_name

    def test_from_dict_with_integer_fields(self):
        """to_push_dict uses integer fields, from_dict should handle both."""
        log = _make_log()
        d = log.to_push_dict()
        d["chain_id"] = 1
        d["chain_name"] = "ethereum"
        restored = Log.from_dict(d)
        assert restored.block_number == log.block_number
        assert restored.log_index == log.log_index

    def test_from_dict_with_metadata_underscore_fields(self):
        d = {
            "address": "0xAb",
            "topics": [],
            "data": "0x",
            "block_number": "0x64",
            "transaction_hash": "0xtx",
            "log_index": "0x0",
            "transaction_index": "0x0",
            "block_hash": "0xbh",
            "removed": False,
            "_chain_id": 42,
            "_chain_name": "testchain",
        }
        log = Log.from_dict(d)
        assert log.chain_id == 42
        assert log.chain_name == "testchain"
        assert log.block_number == 100


# ---------- LogRecorder ----------

class TestLogRecorder:
    def test_write_creates_file(self, tmp_path):
        rec = LogRecorder(
            directory=str(tmp_path / "rec"),
            chain_name="ethereum",
            chain_id=1,
        )
        logs = [_make_log(block_number=100), _make_log(block_number=101)]
        written = rec.write(logs)
        assert written == 2
        rec.close()

        # Find the JSONL file
        jsonl_files = list((tmp_path / "rec").glob("*.jsonl"))
        assert len(jsonl_files) == 1

        # Verify content
        with open(jsonl_files[0]) as f:
            lines = [json.loads(l) for l in f]
        assert len(lines) == 2
        assert lines[0]["block_number"] == "0x64"  # hex format
        assert lines[0]["_chain_id"] == 1
        assert lines[0]["_chain_name"] == "ethereum"

    def test_write_manifest(self, tmp_path):
        rec = LogRecorder(
            directory=str(tmp_path / "rec"),
            chain_name="ethereum",
            chain_id=1,
        )
        rec.write([_make_log(block_number=100), _make_log(block_number=200)])
        path = rec.close()
        assert path is not None

        manifest_files = list((tmp_path / "rec").glob("*.manifest.json"))
        assert len(manifest_files) == 1

        with open(manifest_files[0]) as f:
            manifest = json.load(f)
        assert manifest["chain_name"] == "ethereum"
        assert manifest["chain_id"] == 1
        assert manifest["from_block"] == 100
        assert manifest["to_block"] == 200
        assert manifest["total_logs"] == 2

    def test_empty_write(self, tmp_path):
        rec = LogRecorder(
            directory=str(tmp_path / "rec"),
            chain_name="ethereum",
            chain_id=1,
        )
        written = rec.write([])
        assert written == 0
        path = rec.close()
        assert path is None  # Nothing written, no file created

    def test_multiple_writes(self, tmp_path):
        rec = LogRecorder(
            directory=str(tmp_path / "rec"),
            chain_name="ethereum",
            chain_id=1,
        )
        rec.write([_make_log(block_number=100)])
        rec.write([_make_log(block_number=200)])
        rec.close()

        jsonl_files = list((tmp_path / "rec").glob("*.jsonl"))
        assert len(jsonl_files) == 1
        with open(jsonl_files[0]) as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_context_manager(self, tmp_path):
        with LogRecorder(str(tmp_path / "rec"), "eth", 1) as rec:
            rec.write([_make_log(block_number=50)])
        jsonl_files = list((tmp_path / "rec").glob("*.jsonl"))
        assert len(jsonl_files) == 1


# ---------- LogReplaySource ----------

class TestLogReplaySource:
    def _create_recording(self, tmp_path, chain_name="ethereum", chain_id=1, num_logs=5):
        """Helper: create a recording file and return its path."""
        rec = LogRecorder(
            directory=str(tmp_path / "rec"),
            chain_name=chain_name,
            chain_id=chain_id,
        )
        logs = [_make_log(block_number=100 + i, log_index=i, chain_id=chain_id, chain_name=chain_name) for i in range(num_logs)]
        rec.write(logs)
        path = rec.close()
        return path

    @pytest.mark.asyncio
    async def test_replay_basic(self, tmp_path):
        path = self._create_recording(tmp_path, num_logs=5)
        collected = []

        def callback(logs):
            collected.extend(logs)

        source = LogReplaySource(
            file_path=path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
        )
        total = await source.replay()
        assert total == 5
        assert len(collected) == 5
        # Verify data structure matches original
        assert collected[0].address == "0xDeadBeef"
        assert collected[0].block_number == 100
        assert collected[0].chain_id == 1
        assert collected[0].chain_name == "ethereum"

    @pytest.mark.asyncio
    async def test_replay_with_block_filter(self, tmp_path):
        path = self._create_recording(tmp_path, num_logs=10)
        collected = []

        def callback(logs):
            collected.extend(logs)

        source = LogReplaySource(
            file_path=path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
        )
        total = await source.replay(from_block=103, to_block=106)
        assert total == 4
        assert all(103 <= l.block_number <= 106 for l in collected)

    @pytest.mark.asyncio
    async def test_replay_file_not_found(self, tmp_path):
        source = LogReplaySource(
            file_path="/nonexistent/file.jsonl",
            chain_name="ethereum",
            chain_id=1,
            log_callback=lambda logs: None,
        )
        with pytest.raises(FileNotFoundError):
            await source.replay()

    @pytest.mark.asyncio
    async def test_replay_batching(self, tmp_path):
        path = self._create_recording(tmp_path, num_logs=10)
        batches = []

        def callback(logs):
            batches.append(list(logs))

        source = LogReplaySource(
            file_path=path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
            batch_size=3,
        )
        total = await source.replay()
        assert total == 10
        # Should have 4 batches: 3+3+3+1
        assert len(batches) == 4
        assert len(batches[0]) == 3
        assert len(batches[-1]) == 1

    def test_discover_recordings(self, tmp_path):
        # Create two recordings
        self._create_recording(tmp_path, chain_name="ethereum", chain_id=1, num_logs=3)
        self._create_recording(tmp_path, chain_name="ethereum", chain_id=1, num_logs=2)

        results = LogReplaySource.discover_recordings(str(tmp_path / "rec"))
        assert len(results) >= 2

    def test_discover_recordings_with_chain_filter(self, tmp_path):
        self._create_recording(tmp_path, chain_name="ethereum", chain_id=1, num_logs=1)
        self._create_recording(tmp_path, chain_name="polygon", chain_id=137, num_logs=1)

        results = LogReplaySource.discover_recordings(
            str(tmp_path / "rec"), chain_name="ethereum"
        )
        assert len(results) >= 1
        assert all(r.get("chain_name") == "ethereum" for r in results)

    def test_discover_nonexistent_directory(self, tmp_path):
        results = LogReplaySource.discover_recordings(str(tmp_path / "nonexistent"))
        assert results == []


# ---------- Roundtrip: record then replay ----------

class TestRecordReplayRoundtrip:
    @pytest.mark.asyncio
    async def test_full_roundtrip(self, tmp_path):
        """Record logs, then replay them and verify data integrity."""
        # 1. Record
        rec_dir = str(tmp_path / "recordings")
        rec = LogRecorder(directory=rec_dir, chain_name="ethereum", chain_id=1)

        original_logs = [
            _make_log(block_number=100, log_index=0, tx_hash="0xtx_a"),
            _make_log(block_number=100, log_index=1, tx_hash="0xtx_b"),
            _make_log(block_number=101, log_index=0, tx_hash="0xtx_c"),
            _make_log(block_number=102, log_index=0, tx_hash="0xtx_d"),
        ]
        rec.write(original_logs)
        file_path = rec.close()
        assert file_path is not None

        # 2. Replay
        replayed = []

        def callback(logs):
            replayed.extend(logs)

        source = LogReplaySource(
            file_path=file_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
        )
        total = await source.replay()
        assert total == 4

        # 3. Verify data parity
        for orig, replay in zip(original_logs, replayed):
            assert orig.address == replay.address
            assert orig.topics == replay.topics
            assert orig.data == replay.data
            assert orig.block_number == replay.block_number
            assert orig.transaction_hash == replay.transaction_hash
            assert orig.log_index == replay.log_index
            assert orig.transaction_index == replay.transaction_index
            assert orig.block_hash == replay.block_hash
            assert orig.removed == replay.removed
            assert orig.chain_id == replay.chain_id
            assert orig.chain_name == replay.chain_name

    @pytest.mark.asyncio
    async def test_roundtrip_with_block_filter(self, tmp_path):
        """Record logs, replay a subset, verify only the subset is returned."""
        rec_dir = str(tmp_path / "recordings")
        rec = LogRecorder(directory=rec_dir, chain_name="ethereum", chain_id=1)

        logs = [_make_log(block_number=i) for i in range(100, 110)]
        rec.write(logs)
        file_path = rec.close()

        replayed = []
        source = LogReplaySource(
            file_path=file_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=lambda l: replayed.extend(l),
        )
        total = await source.replay(from_block=103, to_block=106)
        assert total == 4
        assert [l.block_number for l in replayed] == [103, 104, 105, 106]
