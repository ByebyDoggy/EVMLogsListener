"""Logging utilities for EVM Chain Listener."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        if hasattr(record, "chain"):
            log_data["chain"] = record.chain
        
        if hasattr(record, "from_block"):
            log_data["from_block"] = record.from_block
        
        if hasattr(record, "to_block"):
            log_data["to_block"] = record.to_block
        
        if hasattr(record, "log_count"):
            log_data["log_count"] = record.log_count
        
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        
        for key, value in record.__dict__.items():
            if key not in ("name", "msg", "args", "created", "filename", "funcName",
                          "levelname", "lineno", "module", "msecs", "message",
                          "exc_info", "exc_text", "stack_info", "chain",
                          "from_block", "to_block", "log_count", "duration_ms"):
                if not key.startswith("_"):
                    log_data[key] = value
        
        return json.dumps(log_data)


class TextFormatter(logging.Formatter):
    """Text log formatter for human-readable logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as text."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname.ljust(8)
        logger = record.name
        message = record.getMessage()
        
        parts = [f"{timestamp} [{level}] {logger}: {message}"]
        
        if hasattr(record, "chain") and record.chain:
            parts.append(f"chain={record.chain}")
        
        if hasattr(record, "from_block") and record.from_block is not None:
            parts.append(f"from_block={record.from_block}")
        
        if hasattr(record, "to_block") and record.to_block is not None:
            parts.append(f"to_block={record.to_block}")
        
        if hasattr(record, "log_count") and record.log_count is not None:
            parts.append(f"log_count={record.log_count}")
        
        if hasattr(record, "duration_ms") and record.duration_ms is not None:
            parts.append(f"duration_ms={record.duration_ms}")
        
        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        
        return " ".join(parts)


def setup_logging(
    level: str = "INFO",
    log_format: str = "json",
    output: str = "stdout",
    file_path: Optional[str] = None,
) -> logging.Logger:
    """Setup application logging.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_format: Log format (json, text)
        output: Output destination (stdout, file)
        file_path: Path to log file (required if output is file)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("evm_chain_listener")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    if log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter()
    
    if output == "file":
        if not file_path:
            file_path = "evm_chain_listener.log"
        handler = logging.FileHandler(file_path)
    else:
        handler = logging.StreamHandler(sys.stdout)
    
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        Logger instance
    """
    return logging.getLogger(f"evm_chain_listener.{name}")
