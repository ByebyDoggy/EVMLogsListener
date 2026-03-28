"""Main entry point for EVM Chain Listener."""

import argparse
import asyncio
import signal
import sys
from typing import List

from aiohttp import web

from .api.handlers import APIHandler
from .api.routes import create_app
from .cache.log_cache import LogCache
from .chains.base import ChainListener
from .config import load_config
from .models import AppConfig, Log
from .utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


class Application:
    """Main application class."""
    
    def __init__(self, config_path: str):
        """Initialize the application.
        
        Args:
            config_path: Path to configuration file
        """
        self._config = load_config(config_path)
        self._cache = LogCache(self._config.cache)
        self._listeners: List[ChainListener] = []
        self._running = False
        self._app = None
        self._api_task = None
    
    def _on_logs_received(self, logs: List[Log]) -> None:
        """Callback when new logs are received.
        
        Args:
            logs: List of received logs
        """
        added = self._cache.add_many(logs)
        logger.debug(f"Added {added} logs to cache")
    
    def _create_listeners(self) -> None:
        """Create chain listeners from configuration."""
        for chain_config in self._config.chains:
            listener = ChainListener(
                config=chain_config,
                log_callback=self._on_logs_received,
            )
            self._listeners.append(listener)
            logger.info(
                f"Created listener for {chain_config.name} "
                f"(chain_id={chain_config.chain_id})"
            )
    
    async def _start_api_server(self) -> None:
        """Start the API server."""
        handler = APIHandler(
            cache=self._cache,
            listeners=self._listeners,
            config=self._config.api,
            version="1.0.0",
        )
        self._app = create_app(handler)
        
        runner = web.AppRunner(self._app)
        await runner.setup()
        
        site = web.TCPSite(
            runner,
            host=self._config.api.host,
            port=self._config.api.port,
        )
        await site.start()
        
        logger.info(
            f"API server started on {self._config.api.host}:{self._config.api.port}"
        )
    
    async def _start(self) -> None:
        """Start the application."""
        self._running = True
        
        self._create_listeners()
        
        for listener in self._listeners:
            await listener.start()
        
        await self._start_api_server()
        
        logger.info("Application started successfully")
    
    async def _stop(self) -> None:
        """Stop the application gracefully."""
        if not self._running:
            return
        
        logger.info("Shutting down application...")
        self._running = False
        
        for listener in self._listeners:
            await listener.stop()
        
        logger.info("Application stopped")
    
    async def run(self) -> None:
        """Run the application."""
        loop = asyncio.get_running_loop()
        
        stop_event = asyncio.Event()
        
        def signal_handler():
            logger.info("Received shutdown signal")
            stop_event.set()
        
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, signal_handler)
        
        try:
            await self._start()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._stop()


async def async_main(config_path: str) -> None:
    """Async main entry point.
    
    Args:
        config_path: Path to configuration file
    """
    app = Application(config_path)
    await app.run()


def main() -> None:
    """Main entry point."""
    import argparse
    
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
    
    args = parser.parse_args()
    
    setup_logging(
        level="INFO",
        log_format="json",
        output="stdout",
    )
    
    try:
        asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Application error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
