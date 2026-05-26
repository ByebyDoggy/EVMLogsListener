"""Tests for LogDbRecorder and LogDbReplaySource."""

import json
import os
from pathlib import Path

import pytest

from evm_chain_listener.models import Log
from evm_chain_listener.db_recorder import LogDbRecorder
from evm_chain_listener.db_replay import LogDbReplaySource


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


# ---------- LogDbRecorder ----------

class TestLogDbRecorder:
    def test_write_creates_database(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
        )
        logs = [_make_log(block_number=100), _make_log(block_number=101)]
        written = rec.write(logs)
        assert written == 2
        rec.close()

        assert Path(db_path).exists()

    def test_write_updates_session(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
        )
        rec.write([_make_log(block_number=100), _make_log(block_number=200)])
        rec.close()

        # Verify session metadata via discover_sessions
        sessions = LogDbReplaySource.discover_sessions(db_path)
        assert len(sessions) == 1
        assert sessions[0]["chain_name"] == "ethereum"
        assert sessions[0]["chain_id"] == 1
        assert sessions[0]["from_block"] == 100
        assert sessions[0]["to_block"] == 200
        assert sessions[0]["total_logs"] == 2

    def test_empty_write(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
        )
        written = rec.write([])
        assert written == 0
        rec.close()

    def test_multiple_writes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
        )
        rec.write([_make_log(block_number=100)])
        rec.write([_make_log(block_number=200)])
        rec.close()

        sessions = LogDbReplaySource.discover_sessions(db_path)
        assert sessions[0]["total_logs"] == 2

    @pytest.mark.asyncio
    async def test_deduplication(self, tmp_path):
        """INSERT OR IGNORE should skip duplicate logs."""
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
        )
        log = _make_log(block_number=100, log_index=0, tx_hash="0xsame")
        rec.write([log])
        # Write the same log again — should be ignored at DB level
        rec.write([log])
        rec.close()

        # Replay and count
        collected = []
        source = LogDbReplaySource(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=lambda logs: collected.extend(logs),
        )
        total = await source.replay()
        assert total == 1

    def test_context_manager(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with LogDbRecorder(db_path, "eth", 1) as rec:
            rec.write([_make_log(block_number=50)])
        assert Path(db_path).exists()


# ---------- LogDbReplaySource ----------

class TestLogDbReplaySource:
    def _create_recording(self, tmp_path, chain_name="ethereum", chain_id=1, num_logs=5):
        """Helper: create a recording database and return its path."""
        db_path = str(tmp_path / "test.db")
        rec = LogDbRecorder(
            db_path=db_path,
            chain_name=chain_name,
            chain_id=chain_id,
        )
        logs = [_make_log(block_number=100 + i, log_index=i, chain_id=chain_id, chain_name=chain_name) for i in range(num_logs)]
        rec.write(logs)
        rec.close()
        return db_path

    @pytest.mark.asyncio
    async def test_replay_basic(self, tmp_path):
        db_path = self._create_recording(tmp_path, num_logs=5)
        collected = []

        def callback(logs):
            collected.extend(logs)

        source = LogDbReplaySource(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
        )
        total = await source.replay()
        assert total == 5
        assert len(collected) == 5
        # Verify data structure
        assert collected[0].address == "0xDeadBeef"
        assert collected[0].block_number == 100
        assert collected[0].chain_id == 1
        assert collected[0].chain_name == "ethereum"

    @pytest.mark.asyncio
    async def test_replay_with_block_filter(self, tmp_path):
        db_path = self._create_recording(tmp_path, num_logs=10)
        collected = []

        def callback(logs):
            collected.extend(logs)

        source = LogDbReplaySource(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
        )
        total = await source.replay(from_block=103, to_block=106)
        assert total == 4
        assert all(103 <= l.block_number <= 106 for l in collected)

    @pytest.mark.asyncio
    async def test_replay_file_not_found(self, tmp_path):
        source = LogDbReplaySource(
            db_path="/nonexistent/file.db",
            chain_name="ethereum",
            chain_id=1,
            log_callback=lambda logs: None,
        )
        with pytest.raises(FileNotFoundError):
            await source.replay()

    @pytest.mark.asyncio
    async def test_replay_batching(self, tmp_path):
        """Test block-level batching: each batch contains logs from blocks_per_batch blocks."""
        db_path = self._create_recording(tmp_path, num_logs=10)
        batches = []

        def callback(logs):
            batches.append(list(logs))

        # 10 logs at blocks 100-109, blocks_per_batch=3 → batches:
        # [100,101,102], [103,104,105], [106,107,108], [109]
        source = LogDbReplaySource(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=callback,
            blocks_per_batch=3,
            batch_interval_seconds=0,  # no delay in tests
        )
        total = await source.replay()
        assert total == 10
        assert len(batches) == 4
        # First batch: blocks 100,101,102 → 3 logs
        assert len(batches[0]) == 3
        # Last batch: block 109 → 1 log
        assert len(batches[-1]) == 1

    def test_discover_sessions(self, tmp_path):
        db_path = self._create_recording(tmp_path, chain_name="ethereum", chain_id=1, num_logs=3)

        sessions = LogDbReplaySource.discover_sessions(db_path)
        assert len(sessions) == 1
        assert sessions[0]["chain_name"] == "ethereum"
        assert sessions[0]["chain_id"] == 1

    def test_discover_sessions_nonexistent(self, tmp_path):
        sessions = LogDbReplaySource.discover_sessions(str(tmp_path / "nonexistent.db"))
        assert sessions == []


# ---------- Roundtrip: record then replay ----------

class TestRecordReplayRoundtrip:
    @pytest.mark.asyncio
    async def test_full_roundtrip(self, tmp_path):
        """Record logs, then replay them and verify data integrity."""
        # 1. Record
        db_path = str(tmp_path / "recordings" / "logs.db")
        rec = LogDbRecorder(db_path=db_path, chain_name="ethereum", chain_id=1)

        original_logs = [
            _make_log(block_number=100, log_index=0, tx_hash="0xtx_a"),
            _make_log(block_number=100, log_index=1, tx_hash="0xtx_b"),
            _make_log(block_number=101, log_index=0, tx_hash="0xtx_c"),
            _make_log(block_number=102, log_index=0, tx_hash="0xtx_d"),
        ]
        rec.write(original_logs)
        rec.close()

        # 2. Replay
        replayed = []

        def callback(logs):
            replayed.extend(logs)

        source = LogDbReplaySource(
            db_path=db_path,
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
        db_path = str(tmp_path / "recordings" / "logs.db")
        rec = LogDbRecorder(db_path=db_path, chain_name="ethereum", chain_id=1)

        logs = [_make_log(block_number=i) for i in range(100, 110)]
        rec.write(logs)
        rec.close()

        replayed = []
        source = LogDbReplaySource(
            db_path=db_path,
            chain_name="ethereum",
            chain_id=1,
            log_callback=lambda l: replayed.extend(l),
        )
        total = await source.replay(from_block=103, to_block=106)
        assert total == 4
        assert [l.block_number for l in replayed] == [103, 104, 105, 106]
