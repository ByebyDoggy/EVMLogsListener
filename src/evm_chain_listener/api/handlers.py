"""API request handlers."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiohttp import web

from ..cache.log_cache import LogCache
from ..chains.base import ChainListener
from ..models import APIConfig
from ..utils.logging import get_logger

logger = get_logger(__name__)


class APIHandler:
    """Handles HTTP API requests."""
    
    def __init__(
        self,
        cache: LogCache,
        listeners: List[ChainListener],
        config: APIConfig,
        version: str = "1.0.0",
    ):
        """Initialize the API handler.
        
        Args:
            cache: Log cache instance
            listeners: List of chain listeners
            config: API configuration
            version: Application version
        """
        self._cache = cache
        self._listeners = listeners
        self._config = config
        self._version = version
        self._start_time = datetime.now(timezone.utc)
    
    def _get_chain_statuses(self) -> Dict[str, Any]:
        """Get status of all chain listeners."""
        statuses = {}
        for listener in self._listeners:
            status = listener.status
            statuses[status.name] = {
                "status": status.status,
                "last_block": status.last_block,
                "last_poll": status.last_poll.isoformat() if status.last_poll else None,
            }
        return statuses
    
    async def handle_logs(self, request: web.Request) -> web.Response:
        """Handle GET /logs request.
        
        Query parameters:
            chain_id: Filter by chain ID
            from_block: Filter by minimum block number
            to_block: Filter by maximum block number
            from_time: Filter by minimum timestamp (ISO8601)
            to_time: Filter by maximum timestamp (ISO8601)
            address: Filter by contract address
            page: Page number (default 1)
            page_size: Items per page (default 100, max 1000)
        """
        try:
            chain_id_str = request.query.get("chain_id")
            chain_id: Optional[int] = None
            if chain_id_str:
                chain_id = int(chain_id_str)
            
            from_block_str = request.query.get("from_block")
            from_block: Optional[int] = None
            if from_block_str:
                from_block = int(from_block_str)
            
            to_block_str = request.query.get("to_block")
            to_block: Optional[int] = None
            if to_block_str:
                to_block = int(to_block_str)
            
            from_time_str = request.query.get("from_time")
            from_time: Optional[datetime] = None
            if from_time_str:
                from_time = datetime.fromisoformat(from_time_str.replace("Z", "+00:00"))
            
            to_time_str = request.query.get("to_time")
            to_time: Optional[datetime] = None
            if to_time_str:
                to_time = datetime.fromisoformat(to_time_str.replace("Z", "+00:00"))
            
            address = request.query.get("address")
            
            page_str = request.query.get("page", "1")
            page = int(page_str) if page_str else 1
            
            page_size_str = request.query.get("page_size", "100")
            page_size = int(page_size_str) if page_size_str else 100
            
            result = self._cache.query(
                chain_id=chain_id,
                from_block=from_block,
                to_block=to_block,
                from_time=from_time,
                to_time=to_time,
                address=address,
                page=page,
                page_size=page_size,
            )
            
            return web.json_response({
                "status": "success",
                "data": result.to_dict(),
            })
        
        except ValueError as e:
            return web.json_response({
                "status": "error",
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": str(e),
                }
            }, status=400)
        except Exception as e:
            logger.error(f"Error handling logs request: {e}")
            return web.json_response({
                "status": "error",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                }
            }, status=500)
    
    async def handle_logs_stats(self, request: web.Request) -> web.Response:
        """Handle GET /logs/stats request."""
        try:
            stats = self._cache.get_stats()
            
            return web.json_response({
                "status": "success",
                "data": stats.to_dict(),
            })
        
        except Exception as e:
            logger.error(f"Error handling stats request: {e}")
            return web.json_response({
                "status": "error",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                }
            }, status=500)
    
    async def handle_health(self, request: web.Request) -> web.Response:
        """Handle GET /health request."""
        uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        
        chain_statuses = self._get_chain_statuses()
        
        healthy = True
        for listener in self._listeners:
            if not await listener.health_check():
                healthy = False
                break
        
        rpc_nodes = {}
        for listener in self._listeners:
            status = await listener._rpc_pool.health_check()
            rpc_nodes[listener.name] = status.to_dict()
        
        return web.json_response({
            "status": "healthy" if healthy else "degraded",
            "version": self._version,
            "uptime": int(uptime),
            "chains": chain_statuses,
            "rpc_nodes": rpc_nodes,
        })
    
    async def handle_ready(self, request: web.Request) -> web.Response:
        """Handle GET /ready request."""
        checks = {
            "config_loaded": True,
            "rpc_nodes_configured": len(self._listeners) > 0,
        }
        
        ready = all(checks.values())
        
        return web.json_response({
            "ready": ready,
            "checks": checks,
        })
    
    async def handle_root(self, request: web.Request) -> web.Response:
        """Handle GET / request."""
        return web.json_response({
            "name": "EVM Chain Listener",
            "version": self._version,
            "endpoints": [
                "GET /logs",
                "GET /logs/stats",
                "GET /health",
                "GET /ready",
            ],
        })
