"""Main entry point for EVM Chain Listener."""

import argparse
import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cache.log_cache import LogCache
from .chains.base import ChainListener
from .config import load_config
from .models import AppConfig, Log
from .utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

# Global state
_cache: Optional[LogCache] = None
_listeners: List[ChainListener] = []
_config: Optional[AppConfig] = None


def _on_logs_received(logs: List[Log]) -> None:
    """Callback when new logs are received."""
    global _cache
    if _cache:
        added = _cache.add_many(logs)
        logger.debug(f"Added {added} logs to cache")


def _create_listeners() -> None:
    """Create chain listeners from configuration."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global _cache, _listeners, _config
    
    # Startup
    logger.info("Starting application...")
    _cache = LogCache(_config.cache)
    _create_listeners()
    
    for listener in _listeners:
        await listener.start()
    
    logger.info("Application started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application...")
    for listener in _listeners:
        await listener.stop()
    logger.info("Application stopped")


# Create FastAPI app
app = FastAPI(
    title="EVM Chain Listener",
    description="Lightweight EVM-compatible chain log listener",
    version="1.0.0",
    lifespan=lifespan,
)

# Setup templates
templates_path = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))


# API Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page."""
    return templates.TemplateResponse(request, "index.html", {})


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
    
    # Apply topic filter if specified
    logs = result.items
    if topic:
        logs = [log for log in logs if log.topics and log.topics[0] == topic]
    
    return {
        "status": "success",
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
    return {
        "status": "success",
        "data": stats.to_dict()
    }


@app.get("/api/logs/topics")
async def get_unique_topics():
    """Get unique event topics from cached logs."""
    global _cache
    topics = _cache.get_unique_topics()
    return {
        "status": "success",
        "data": {"topics": topics}
    }


@app.get("/api/chains")
async def get_chains():
    """Get list of configured chains and their status."""
    global _listeners
    chains = []
    for listener in _listeners:
        status = listener.status
        chains.append({
            "name": status.name,
            "chain_id": status.chain_id,
            "status": status.status,
            "last_block": status.last_block,
            "last_poll": status.last_poll.isoformat() if status.last_poll else None,
        })
    return {
        "status": "success",
        "data": {"chains": chains}
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    global _listeners
    
    uptime = 0
    chain_statuses = {}
    
    for listener in _listeners:
        status = listener.status
        chain_statuses[status.name] = {
            "status": status.status,
            "last_block": status.last_block,
            "last_poll": status.last_poll.isoformat() if status.last_poll else None,
        }
    
    healthy = all(l.status.status == "running" for l in _listeners)
    
    rpc_nodes = {}
    for listener in _listeners:
        node_status = await listener._rpc_pool.health_check()
        rpc_nodes[listener.name] = node_status.to_dict()
    
    return {
        "status": "healthy" if healthy else "degraded",
        "version": "1.0.0",
        "chains": chain_statuses,
        "rpc_nodes": rpc_nodes,
    }


@app.get("/ready")
async def readiness_check():
    """Readiness check endpoint."""
    global _listeners
    checks = {
        "config_loaded": True,
        "rpc_nodes_configured": len(_listeners) > 0,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
    }


def main() -> None:
    """Main entry point."""
    global _config
    
    parser = argparse.ArgumentParser(
        description="EVM Chain Listener - Lightweight EVM-compatible chain log listener"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version="%(prog)s 1.0.0"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override API host"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override API port"
    )
    
    args = parser.parse_args()
    
    setup_logging(
        level="INFO",
        log_format="json",
        output="stdout",
    )
    
    # Load configuration
    _config = load_config(args.config)
    
    host = args.host or _config.api.host
    port = args.port or _config.api.port
    
    logger.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # Use our own logging
    )


if __name__ == "__main__":
    main()
