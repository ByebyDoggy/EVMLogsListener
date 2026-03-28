"""API routes configuration."""

from aiohttp import web

from .handlers import APIHandler


def create_app(handler: APIHandler) -> web.Application:
    """Create and configure the aiohttp application.
    
    Args:
        handler: APIHandler instance
    
    Returns:
        Configured aiohttp application
    """
    app = web.Application()
    
    app.router.add_get("/", handler.handle_root)
    app.router.add_get("/logs", handler.handle_logs)
    app.router.add_get("/logs/stats", handler.handle_logs_stats)
    app.router.add_get("/health", handler.handle_health)
    app.router.add_get("/ready", handler.handle_ready)
    
    return app
