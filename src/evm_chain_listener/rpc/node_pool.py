"""RPC Node Pool for managing multiple RPC nodes."""

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from ..models import Log, NodeHealthStatus, RPCNodeConfig
from ..utils.logging import get_logger
from .exceptions import (
    AllNodesFailedError,
    InvalidResponseError,
    LogLimitExceededError,
    RateLimitError,
    TimeoutError,
    parse_rpc_error,
)

logger = get_logger(__name__)


class RPCNode:
    """Represents a single RPC node with health tracking."""
    
    def __init__(self, config: RPCNodeConfig):
        self.config = config
        self.url = config.url
        self.priority = config.priority
        self.node_type = config.node_type
        self._is_healthy = True
        self._consecutive_failures = 0
        self._last_request_time = 0.0
        self._rate_limit_delay = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
    
    @property
    def is_healthy(self) -> bool:
        """Check if node is healthy."""
        return self._is_healthy
    
    def mark_healthy(self) -> None:
        """Mark node as healthy."""
        self._is_healthy = True
        self._consecutive_failures = 0
    
    def mark_unhealthy(self) -> None:
        """Mark node as unhealthy."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self._is_healthy = False
            logger.warning(f"Node {self.url} marked as unhealthy after {self._consecutive_failures} failures")
    
    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure aiohttp session exists."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _throttle(self) -> None:
        """Apply rate limiting throttling."""
        if self._rate_limit_delay > 0:
            await asyncio.sleep(self._rate_limit_delay)
        
        now = time.time()
        if self.config.rate_limit:
            min_interval = 1.0 / self.config.rate_limit.requests_per_second
            time_since_last = now - self._last_request_time
            if time_since_last < min_interval:
                await asyncio.sleep(min_interval - time_since_last)
        
        self._last_request_time = time.time()
    
    async def request(
        self,
        method: str,
        params: Optional[List[Any]] = None,
    ) -> Any:
        """Make an RPC request to the node.
        
        Args:
            method: RPC method name
            params: RPC method parameters
        
        Returns:
            RPC response result
        
        Raises:
            RateLimitError: When rate limit is exceeded
            TimeoutError: When request times out
            InvalidResponseError: When response is invalid
            RPCError: For other RPC errors
        """
        await self._throttle()
        
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": 1,
        }
        
        session = await self._ensure_session()
        
        try:
            async with session.post(self.url, json=payload) as response:
                if response.status == 429:
                    data = await response.json()
                    raise RateLimitError(
                        message=f"Rate limit exceeded for {self.url}",
                        node_url=self.url,
                    )
                
                if response.status != 200:
                    raise InvalidResponseError(
                        message=f"HTTP {response.status} from {self.url}",
                        node_url=self.url,
                    )
                
                data = await response.json()
                
                if "error" in data:
                    error = data["error"]
                    if isinstance(error, dict):
                        raise parse_rpc_error(error, self.url)
                    raise InvalidResponseError(
                        message=f"RPC error: {error}",
                        node_url=self.url,
                    )
                
                return data.get("result")
        
        except asyncio.TimeoutError:
            raise TimeoutError(message=f"Timeout requesting {method}", node_url=self.url)
        except aiohttp.ClientError as e:
            raise InvalidResponseError(
                message=f"Connection error: {e}",
                node_url=self.url,
            )
    
    async def eth_get_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
        chain_id: int = 0,
        chain_name: str = "",
    ) -> List[Log]:
        """Get logs from the RPC node.
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
            address: Contract address to filter
            topics: Event topics to filter
            chain_id: Chain ID for Log object
            chain_name: Chain name for Log object
        
        Returns:
            List of Log objects
        
        Raises:
            LogLimitExceededError: When log filter is too large
            RateLimitError: When rate limit is exceeded
        """
        params: Dict[str, Any] = {
            "fromBlock": hex(from_block) if isinstance(from_block, int) else from_block,
            "toBlock": hex(to_block) if isinstance(to_block, int) else to_block,
        }
        
        if address:
            params["address"] = address
        
        if topics:
            params["topics"] = topics
        
        result = await self.request("eth_getLogs", [params])
        
        if result is None:
            return []
        
        logs = []
        for log_data in result:
            logs.append(Log.from_rpc_response(log_data, chain_id, chain_name))
        
        return logs
    
    async def eth_block_number(self) -> int:
        """Get the latest block number.
        
        Returns:
            Current block number
        """
        result = await self.request("eth_blockNumber", [])
        
        if result is None:
            raise InvalidResponseError(
                message="eth_blockNumber returned null",
                node_url=self.url,
            )
        
        return int(result, 16)
    
    async def health_check(self) -> bool:
        """Perform a health check on the node.
        
        Returns:
            True if node is healthy, False otherwise
        """
        try:
            await self.eth_block_number()
            self.mark_healthy()
            return True
        except Exception:
            self.mark_unhealthy()
            return False


class RPCNodePool:
    """Manages multiple RPC nodes with failover support."""
    
    def __init__(self, nodes: List[RPCNodeConfig], chain_id: int = 0, chain_name: str = ""):
        self.chain_id = chain_id
        self.chain_name = chain_name
        self._nodes = sorted([RPCNode(cfg) for cfg in nodes], key=lambda n: n.priority)
        self._current_index = 0
        self._active_node: Optional[RPCNode] = None
        self._lock = asyncio.Lock()
    
    async def close(self) -> None:
        """Close all node sessions."""
        for node in self._nodes:
            await node.close()
    
    def _get_available_nodes(self) -> List[RPCNode]:
        """Get list of available nodes in priority order.
        
        Returns:
            List of healthy nodes
        """
        available = [n for n in self._nodes if n.is_healthy]
        
        if not available:
            logger.warning(f"All nodes unhealthy for chain {self.chain_name}, using all nodes")
            return self._nodes
        
        return available
    
    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[List[str]] = None,
    ) -> List[Log]:
        """Get logs using the next available node.
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
            address: Contract address to filter
            topics: Event topics to filter
        
        Returns:
            List of Log objects
        
        Raises:
            AllNodesFailedError: When all nodes fail
        """
        nodes_to_try = self._get_available_nodes()
        
        for node in nodes_to_try:
            try:
                return await node.eth_get_logs(
                    from_block=from_block,
                    to_block=to_block,
                    address=address,
                    topics=topics,
                    chain_id=self.chain_id,
                    chain_name=self.chain_name,
                )
            except RateLimitError:
                logger.warning(f"Rate limit hit on node {node.url}")
                node.mark_unhealthy()
                await asyncio.sleep(1)
                continue
            except LogLimitExceededError:
                raise
            except Exception as e:
                logger.error(f"Error on node {node.url}: {e}")
                node.mark_unhealthy()
                continue
        
        raise AllNodesFailedError(f"All RPC nodes failed for chain {self.chain_name}")
    
    async def get_block_number(self) -> int:
        """Get the latest block number.
        
        Returns:
            Current block number
        
        Raises:
            AllNodesFailedError: When all nodes fail
        """
        nodes_to_try = self._get_available_nodes()
        
        for node in nodes_to_try:
            try:
                return await node.eth_block_number()
            except Exception as e:
                logger.error(f"Error getting block number from {node.url}: {e}")
                node.mark_unhealthy()
                continue
        
        raise AllNodesFailedError(f"All RPC nodes failed for chain {self.chain_name}")
    
    async def health_check(self) -> NodeHealthStatus:
        """Check health status of all nodes.
        
        Returns:
            NodeHealthStatus with health information
        """
        await asyncio.gather(*[node.health_check() for node in self._nodes])
        
        healthy_nodes = [n for n in self._nodes if n.is_healthy]
        
        active_url = ""
        if healthy_nodes:
            self._active_node = healthy_nodes[0]
            active_url = self._active_node.url
        
        return NodeHealthStatus(
            active=active_url,
            available=[n.url for n in healthy_nodes],
            failed=[n.url for n in self._nodes if not n.is_healthy],
        )
