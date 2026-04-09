"""Main entry point for EVM Chain Listener."""

import argparse
import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cache.log_cache import LogCache
from .chains.base import ChainListener
from .chains.backtest import BacktestRunner
from .config import load_config
from .models import (
    AppConfig,
    BacktestConfig as BacktestConfigModel,
    BacktestTaskStatus,
    Log,
)
from .pusher import LogPusher
from .recorder import LogRecorder
from .replay import LogReplaySource
from .utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

# Global state
_cache: Optional[LogCache] = None
_listeners: List[ChainListener] = []
_backtest_runners: Dict[str, BacktestRunner] = {}  # chain_name_or_id -> BacktestRunner
_pusher: Optional[LogPusher] = None
_recorders: Dict[str, LogRecorder] = {}  # chain_name -> LogRecorder
_config: Optional[AppConfig] = None
_mode: str = "realtime"  # "realtime" or "backtest"
_cli_from_block: Optional[int] = None  # CLI override for backtest from_block
_cli_to_block: Optional[int] = None    # CLI override for backtest to_block


def _on_logs_received(logs: List[Log]) -> None:
    """Callback when new logs are received -> write to cache + optionally push + record."""
    global _cache, _pusher, _recorders
    if _cache:
        added = _cache.add_many(logs)
        if added > 0:
            logger.debug(
                f"Added {added} logs to cache",
                extra={"log_count": added},
            )
    # Feed to pusher (async fire-and-forget from sync callback)
    if _pusher and _pusher.enabled:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_pusher.on_new_logs(logs))
        except RuntimeError:
            pass
    # Record to local file
    if _recorders and logs:
        # Group by chain_name and write to the appropriate recorder
        by_chain: Dict[str, List[Log]] = {}
        for log in logs:
            cname = log.chain_name or "unknown"
            by_chain.setdefault(cname, []).append(log)
        for cname, chain_logs in by_chain.items():
            rec = _recorders.get(cname)
            if rec:
                rec.write(chain_logs)


def _create_listeners() -> None:
    """Create chain listeners from configuration (realtime mode)."""
    global _listeners, _config
    for chain_config in _config.chains:
        listener = ChainListener(
            config=chain_config,
            log_callback=_on_logs_received,
        )
        _listeners.append(listener)
        logger.info(
            f"Created listener for {chain_config.name} "
            f"(chain_id={chain_config.chain_id})"
        )


def _create_backtest_runners(use_rpc: bool = True) -> None:
    """Create backtest runners for each configured chain.

    Args:
        use_rpc: If True, create runners with RPC pool. If False, create
                 lightweight runners without RPC (used for file-based replay).
    """
    global _backtest_runners, _config
    for chain_config in _config.chains:
        runner = BacktestRunner(
            chain_config=chain_config,
            log_callback=_on_logs_received,
            use_rpc=use_rpc,
        )
        _backtest_runners[chain_config.name] = runner
        _backtest_runners[str(chain_config.chain_id)] = runner
        logger.info(
            f"Created backtest runner for {chain_config.name} "
            f"(chain_id={chain_config.chain_id}, rpc={'on' if use_rpc else 'off'})"
        )


def _find_backtest_runner(chain_name_or_id: str) -> Optional[BacktestRunner]:
    """Find a backtest runner by chain name or ID."""
    if chain_name_or_id in _backtest_runners:
        return _backtest_runners[chain_name_or_id]
    for key, runner in _backtest_runners.items():
        if key.lower() == chain_name_or_id.lower():
            return runner
    for runner in _backtest_runners.values():
        if str(runner.chain_config.chain_id) == chain_name_or_id:
            return runner
        if runner.chain_config.name.lower() == chain_name_or_id.lower():
            return runner
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global _cache, _listeners, _backtest_runners, _config, _pusher, _recorders

    # Startup
    logger.info(f"Starting application in {_mode.upper()} mode...")
    _cache = LogCache(_config.cache)

    # Init pusher before listeners so it's ready when first logs arrive
    _pusher = LogPusher(config=_config.alert_processor, chains=_config.chains)

    # Init recorders if recording is enabled
    if _config.recorder.enabled:
        for chain_config in _config.chains:
            rec = LogRecorder(
                directory=_config.recorder.directory,
                chain_name=chain_config.name,
                chain_id=chain_config.chain_id,
            )
            _recorders[chain_config.name] = rec
            logger.info(
                f"[recorder] Recording enabled for {chain_config.name} "
                f"→ {_config.recorder.directory}",
                extra={"chain": chain_config.name},
            )

    if _mode == "realtime":
        _create_listeners()
        for listener in _listeners:
            await listener.start()
    else:
        # Backtest mode: check if replay from file is configured
        if _config.replay.file_path or _config.replay.directory:
            _create_backtest_runners(use_rpc=False)
            await _start_replay()
        else:
            _create_backtest_runners(use_rpc=True)
            # Auto-submit backtest task if startup config is set
            await _auto_submit_backtest()

    # Start pusher after everything else is ready
    await _pusher.start()

    # Reconnect gap check on startup
    if (_pusher.enabled
            and _config.alert_processor.reconnect_check_on_startup):
        await _check_and_replay_gaps()

    logger.info("Application started successfully")

    yield

    # Shutdown
    logger.info("Shutting down application...")
    if _pusher:
        await _pusher.stop()
    # Close recorders
    for rec in _recorders.values():
        rec.close()
    _recorders.clear()
    if _mode == "realtime":
        for listener in _listeners:
            await listener.stop()
    else:
        for runner in set(_backtest_runners.values()):
            await runner.close()
    logger.info("Application stopped")


async def _auto_submit_backtest() -> None:
    """Auto-submit backtest tasks on startup if from_block/to_block are configured.

    Priority: CLI args > config file backtest section > nothing.
    For each configured chain, a separate backtest task is submitted.
    """
    global _config, _backtest_runners, _cli_from_block, _cli_to_block

    from_block = _cli_from_block or _config.backtest.from_block
    to_block = _cli_to_block or _config.backtest.to_block

    if from_block is None or to_block is None:
        logger.info(
            "[backtest] No auto-start block range configured. "
            "Use --from-block/--to-block CLI args or the 'backtest' config section. "
            "You can also submit tasks via API: POST /api/backtest/run"
        )
        return

    batch_size = _config.backtest.batch_size

    for chain_config in _config.chains:
        runner = _backtest_runners.get(chain_config.name)
        if not runner:
            continue

        config = BacktestConfigModel(
            chain_name_or_id=chain_config.name,
            from_block=from_block,
            to_block=to_block,
            batch_size=batch_size,
        )
        try:
            task_status = await runner.submit_backtest(config)
            logger.info(
                f"[backtest] Auto-submitted task for {chain_config.name}: "
                f"blocks [{from_block}, {to_block}], batch_size={batch_size}, "
                f"task_id={task_status.task_id}",
                extra={
                    "chain": chain_config.name,
                    "from_block": from_block,
                    "to_block": to_block,
                    "task_id": task_status.task_id,
                },
            )
        except Exception as e:
            logger.error(
                f"[backtest] Failed to auto-submit task for {chain_config.name}: {e}",
                extra={"chain": chain_config.name},
            )


async def _check_and_replay_gaps() -> None:
    """Check AlertProcessor status on startup; replay any gaps."""
    global _pusher, _cache
    if not _pusher or not _cache:
        return

    status_data = await _pusher.check_reconnect_status()
    if not status_data:
        logger.warning("[reconnect] Could not reach AlertProcessor, skipping gap check")
        return

    consumed = status_data.get("consumed_blocks", {})
    if not consumed:
        return

    for chain_id_str, info in consumed.items():
        try:
            cid = int(chain_id_str)
        except (ValueError, TypeError):
            continue
        remote_last_block = info.get("last_block", 0)
        if remote_last_block <= 0:
            continue
        # Find local logs that are newer than remote's last block
        gap_result = _cache.query(
            chain_id=cid,
            from_block=remote_last_block + 1,
            page=1,
            page_size=10000,
        )
        if gap_result.total > 0:
            gap_logs = gap_result.items
            max_block = max(l.block_number for l in gap_logs)
            min_block = min(l.block_number for l in gap_logs)
            logger.info(
                "[reconnect] Found gap for chain %s: blocks [%d,%d], %d logs",
                chain_id_str, min_block, max_block, len(gap_logs),
                extra={
                    "chain": chain_id_str,
                    "from_block": min_block,
                    "to_block": max_block,
                    "log_count": len(gap_logs),
                },
            )
            await _pusher.send_replay(cid, min_block, max_block, gap_logs)


async def _start_replay() -> None:
    """Start replaying logs from local files based on replay config."""
    global _config, _backtest_runners

    replay_cfg = _config.replay
    chain_configs = {c.name: c for c in _config.chains}

    if replay_cfg.file_path:
        # Single file mode: replay one file for the first configured chain
        chain = _config.chains[0] if _config.chains else None
        if not chain:
            logger.error("[replay] No chains configured, cannot replay")
            return

        source = LogReplaySource(
            file_path=replay_cfg.file_path,
            chain_name=chain.name,
            chain_id=chain.chain_id,
            log_callback=_on_logs_received,
        )

        async def _do_replay():
            try:
                total = await source.replay(
                    from_block=replay_cfg.from_block,
                    to_block=replay_cfg.to_block,
                )
                logger.info(
                    f"[replay] File replay complete: {total} logs from {replay_cfg.file_path}",
                )
            except Exception as e:
                logger.error(f"[replay] File replay failed: {e}")

        asyncio.create_task(_do_replay())

    elif replay_cfg.directory:
        # Directory mode: discover and replay all matching recordings
        async def _do_directory_replay():
            for chain_config in _config.chains:
                recordings = LogReplaySource.discover_recordings(
                    replay_cfg.directory,
                    chain_name=chain_config.name,
                )
                for rec_info in recordings:
                    filename = rec_info.get("file")
                    if not filename:
                        continue
                    file_path = str(Path(replay_cfg.directory) / filename)
                    try:
                        source = LogReplaySource(
                            file_path=file_path,
                            chain_name=chain_config.name,
                            chain_id=chain_config.chain_id,
                            log_callback=_on_logs_received,
                        )
                        total = await source.replay(
                            from_block=replay_cfg.from_block,
                            to_block=replay_cfg.to_block,
                        )
                        logger.info(
                            f"[replay] Replayed {total} logs from {filename} "
                            f"for chain {chain_config.name}",
                        )
                    except Exception as e:
                        logger.error(f"[replay] Failed to replay {filename}: {e}")

        asyncio.create_task(_do_directory_replay())


# Create FastAPI app
app = FastAPI(
    title="EVM Chain Listener",
    description="Lightweight EVM-compatible chain log listener (Realtime & Backtest mode)",
    version="1.0.0",
    lifespan=lifespan,
)

# Setup templates
templates_path = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))


# ==================== Web UI Route ====================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page."""
    return templates.TemplateResponse(request, "index.html", {})


# ==================== Log Query APIs ====================
@app.get("/api/logs")
async def get_logs(
    chain_id: Optional[int] = Query(None, description="Filter by chain ID"),
    from_block: Optional[int] = Query(None, description="Minimum block number"),
    to_block: Optional[int] = Query(None, description="Maximum block number"),
    from_time: Optional[str] = Query(None, description="Start time (ISO8601)"),
    to_time: Optional[str] = Query(None, description="End time (ISO8601)"),
    address: Optional[str] = Query(None, description="Contract address"),
    topic: Optional[str] = Query(None, description="Event topic (first topic)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(100, ge=1, le=1000, description="Items per page"),
):
    """Query logs from cache with filters."""
    global _cache

    from_time_dt = None
    to_time_dt = None
    if from_time:
        from_time_dt = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
    if to_time:
        to_time_dt = datetime.fromisoformat(to_time.replace("Z", "+00:00"))

    result = _cache.query(
        chain_id=chain_id,
        from_block=from_block,
        to_block=to_block,
        from_time=from_time_dt,
        to_time=to_time_dt,
        address=address,
        page=page,
        page_size=page_size,
    )

    logs = result.items
    if topic:
        logs = [log for log in logs if log.topics and log.topics[0] == topic]

    return {
        "status": "success",
        "mode": _mode,
        "data": {
            "logs": [log.to_dict() for log in logs],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
                "total_pages": result.total_pages,
            }
        }
    }


@app.get("/api/logs/stats")
async def get_logs_stats():
    """Get cache statistics."""
    global _cache
    stats = _cache.get_stats()
    return {"status": "success", "data": stats.to_dict()}


@app.get("/api/logs/topics")
async def get_unique_topics():
    """Get unique event topics from cached logs."""
    global _cache
    topics = _cache.get_unique_topics()
    return {"status": "success", "data": {"topics": topics}}


# ==================== Chains Status APIs ====================
@app.get("/api/chains")
async def get_chains():
    """Get list of configured chains and their status."""
    global _listeners, _backtest_runners, _mode
    chains = []

    if _mode == "realtime":
        for listener in _listeners:
            s = listener.status
            chains.append({
                "name": s.name,
                "chain_id": s.chain_id,
                "status": s.status,
                "last_block": s.last_block,
                "last_poll": s.last_poll.isoformat() if s.last_poll else None,
            })
    else:
        for runner in set(_backtest_runners.values()):
            healthy = await runner.health_check()
            chains.append({
                "name": runner.chain_config.name,
                "chain_id": runner.chain_config.chain_id,
                "status": "ready" if healthy else "unhealthy",
                "last_block": None,
                "last_poll": None,
            })

    return {"status": "success", "mode": _mode, "data": {"chains": chains}}


# ==================== Pusher Stats API ====================
@app.get("/api/pusher/stats")
async def get_pusher_stats():
    """Get LogPusher statistics."""
    global _pusher
    if not _pusher:
        return {"status": "success", "data": None}
    return {"status": "success", "data": _pusher.stats.to_dict()}


# ==================== Backtest APIs ====================
@app.post("/api/backtest/run")
async def run_backtest(
    chain: str = Query(..., description="Chain name or chain ID"),
    from_block: int = Query(..., description="Starting block number (inclusive)"),
    to_block: int = Query(..., description="Ending block number (inclusive)"),
    batch_size: int = Query(1000, ge=10, le=100000, description="Blocks per batch query"),
):
    """Submit a backtest task.

    Queries historical logs from the specified block range using RPC pool.
    The task runs asynchronously; use /api/backtest/status/{task_id} to poll progress.
    """
    global _backtest_runners

    runner = _find_backtest_runner(chain)
    if runner is None:
        available = list({r.chain_config.name for r in set(_backtest_runners.values())})
        raise HTTPException(status_code=404, detail=f"Chain '{chain}' not found. Available: {available}")

    config = BacktestConfigModel(
        chain_name_or_id=chain,
        from_block=from_block,
        to_block=to_block,
        batch_size=batch_size,
    )
    task_status = await runner.submit_backtest(config)
    return {"status": "submitted", "data": task_status.to_dict()}


@app.get("/api/backtest/status/{task_id}")
async def get_backtest_status(task_id: str):
    """Get the status of a backtest task."""
    global _backtest_runners
    for runner in set(_backtest_runners.values()):
        ts = runner.get_task_status(task_id)
        if ts:
            return {"status": "success", "data": ts.to_dict()}
    raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")


@app.get("/api/backtest/tasks")
async def list_backtest_tasks():
    """List all backtest tasks."""
    global _backtest_runners
    all_tasks = []
    seen = set()
    for runner in set(_backtest_runners.values()):
        for t in runner.list_tasks():
            if t.task_id not in seen:
                all_tasks.append(t.to_dict())
                seen.add(t.task_id)
    return {"status": "success", "data": {"tasks": all_tasks}}


@app.get("/api/backtest/chains")
async def list_backtest_chains():
    """List available chains for backtesting."""
    global _backtest_runners
    chains = [
        {"name": r.chain_config.name, "chain_id": r.chain_config.chain_id}
        for r in set(_backtest_runners.values())
    ]
    return {"status": "success", "data": {"chains": chains}}


# ==================== Replay APIs ====================
@app.get("/api/replay/recordings")
async def list_recordings(
    directory: Optional[str] = Query(None, description="Directory to scan for recordings"),
):
    """List available recording files for replay."""
    global _config
    scan_dir = directory or _config.replay.directory or _config.recorder.directory
    if not scan_dir:
        return {"status": "success", "data": {"recordings": [], "message": "No recording directory configured"}}
    recordings = LogReplaySource.discover_recordings(scan_dir)
    return {"status": "success", "data": {"recordings": recordings, "directory": scan_dir}}


@app.post("/api/replay/start")
async def start_replay(
    file_path: Optional[str] = Query(None, description="JSONL file to replay"),
    chain: Optional[str] = Query(None, description="Chain name or ID"),
    from_block: Optional[int] = Query(None, description="Start block filter"),
    to_block: Optional[int] = Query(None, description="End block filter"),
):
    """Start replaying logs from a local recording file."""
    global _config, _backtest_runners

    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")

    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    # Determine chain config
    chain_config = _config.chains[0] if _config.chains else None
    if chain:
        runner = _find_backtest_runner(chain)
        if runner:
            chain_config = runner.chain_config
    if not chain_config:
        raise HTTPException(status_code=400, detail="No chain configuration available")

    source = LogReplaySource(
        file_path=file_path,
        chain_name=chain_config.name,
        chain_id=chain_config.chain_id,
        log_callback=_on_logs_received,
    )

    async def _do_replay():
        try:
            total = await source.replay(from_block=from_block, to_block=to_block)
            logger.info(f"[replay] API-triggered replay complete: {total} logs from {file_path}")
        except Exception as e:
            logger.error(f"[replay] API-triggered replay failed: {e}")

    asyncio.create_task(_do_replay())

    return {
        "status": "started",
        "data": {
            "file_path": file_path,
            "chain": chain_config.name,
            "from_block": from_block,
            "to_block": to_block,
        },
    }


# ==================== Health Check APIs ====================
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    global _listeners, _backtest_runners, _mode, _pusher

    chain_statuses = {}

    if _mode == "realtime":
        for listener in _listeners:
            s = listener.status
            chain_statuses[s.name] = {
                "status": s.status,
                "last_block": s.last_block,
                "last_poll": s.last_poll.isoformat() if s.last_poll else None,
            }
        healthy = all(l.status.status == "running" for l in _listeners)
        rpc_nodes = {}
        for listener in _listeners:
            ns = await listener._rpc_pool.health_check()
            rpc_nodes[listener.name] = ns.to_dict()
    else:
        h_checks = []
        for runner in set(_backtest_runners.values()):
            ok = await runner.health_check()
            chain_statuses[runner.chain_config.name] = {
                "status": "ready" if ok else "unhealthy",
                "last_block": None,
                "last_poll": None,
            }
            h_checks.append(ok)
            if runner.rpc_pool:
                ns = await runner.rpc_pool.health_check()
                rpc_nodes[runner.chain_config.name] = ns.to_dict()
            else:
                rpc_nodes[runner.chain_config.name] = {"mode": "file_replay"}
        healthy = all(h_checks) if h_checks else True

    pusher_info = None
    if _pusher and _pusher.enabled:
        pusher_info = _pusher.stats.to_dict()

    return {
        "status": "healthy" if healthy else "degraded",
        "version": "1.0.0",
        "mode": _mode,
        "chains": chain_statuses,
        "rpc_nodes": rpc_nodes,
        "pusher": pusher_info,
    }


@app.get("/ready")
async def readiness_check():
    """Readiness check endpoint."""
    global _listeners, _backtest_runners, _mode, _pusher
    checks = {
        "config_loaded": True,
        "mode": _mode,
    }
    if _mode == "realtime":
        checks["rpc_nodes_configured"] = len(_listeners) > 0
    else:
        checks["rpc_nodes_configured"] = len(_backtest_runners) > 0
    if _pusher:
        checks["pusher_ready"] = _pusher.enabled
    else:
        checks["pusher_ready"] = True  # Not configured counts as ready
    return {"ready": all(checks.values()), "checks": checks}


# ==================== CLI Entry Point ====================
def main() -> None:
    """Main entry point."""
    global _config, _mode, _cli_from_block, _cli_to_block

    parser = argparse.ArgumentParser(
        description="EVM Chain Listener - Lightweight EVM-compatible chain log listener"
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to configuration file")
    parser.add_argument(
        "-m", "--mode", choices=["realtime", "backtest"], default="realtime",
        help="Run mode (default: realtime)",
    )
    parser.add_argument("-v", "--version", action="version", version="%(prog)s 1.0.0")
    parser.add_argument("--host", default=None, help="Override API host")
    parser.add_argument("--port", type=int, default=None, help="Override API port")
    parser.add_argument(
        "--from-block", type=int, default=None,
        help="Backtest start block (overrides config file)",
    )
    parser.add_argument(
        "--to-block", type=int, default=None,
        help="Backtest end block (overrides config file)",
    )

    args = parser.parse_args()
    _mode = args.mode
    _cli_from_block = args.from_block
    _cli_to_block = args.to_block

    setup_logging(level="INFO", log_format="json", output="stdout")

    _config = load_config(args.config)

    host = args.host or _config.api.host
    port = args.port or _config.api.port

    logger.info(f"Starting server on {host}:{port} in {_mode} mode")

    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
