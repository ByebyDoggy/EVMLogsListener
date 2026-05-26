"""Configuration loader for EVM Chain Listener."""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .models import (
    AlertProcessorConfig,
    APIConfig,
    AppConfig,
    BacktestStartupConfig,
    CacheConfig,
    ChainConfig,
    HealthConfig,
    LogConfig,
    RateLimit,
    RecorderConfig,
    ReplayConfig,
    RPCNodeConfig,
)


class ConfigError(Exception):
    """Configuration error exception."""
    pass


def expand_env_vars(value: Any) -> Any:
    """Expand environment variables in configuration values.
    
    Supports ${VAR_NAME} syntax.
    """
    if isinstance(value, str):
        pattern = r'\$\{([^}]+)\}'
        
        def replace_env_var(match):
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                return match.group(0)
            return env_value
        
        return re.sub(pattern, replace_env_var, value)
    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def load_yaml_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    
    if not path.is_file():
        raise ConfigError(f"Configuration path is not a file: {config_path}")
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML configuration: {e}")
    
    if config is None:
        raise ConfigError(f"Empty configuration file: {config_path}")
    
    return expand_env_vars(config)


def parse_rpc_node(node_data: Dict[str, Any]) -> RPCNodeConfig:
    """Parse RPC node configuration."""
    url = node_data.get('url')
    if not url:
        raise ConfigError("RPC node URL is required")
    
    rate_limit_data = node_data.get('rate_limit')
    rate_limit = None
    if rate_limit_data:
        rate_limit = RateLimit(
            requests_per_second=rate_limit_data.get('requests_per_second', 10),
            burst=rate_limit_data.get('burst', 20),
        )
    
    return RPCNodeConfig(
        url=url,
        priority=node_data.get('priority', 100),
        node_type=node_data.get('type', 'public'),
        rate_limit=rate_limit,
    )


def parse_chain_config(chain_data: Dict[str, Any]) -> ChainConfig:
    """Parse chain configuration."""
    name = chain_data.get('name')
    if not name:
        raise ConfigError("Chain name is required")
    
    chain_id = chain_data.get('chain_id')
    if chain_id is None:
        raise ConfigError(f"Chain ID is required for chain '{name}'")
    
    rpc_nodes = []
    for node_data in chain_data.get('rpc_nodes', []):
        try:
            rpc_nodes.append(parse_rpc_node(node_data))
        except ConfigError as e:
            raise ConfigError(f"Invalid RPC node configuration: {e}")
    
    if not rpc_nodes:
        # apipool_server can serve as alternative source of RPC endpoints
        if not chain_data.get('apipool_server'):
            raise ConfigError(f"At least one RPC node or apipool_server is required for chain '{name}'")
    
    return ChainConfig(
        name=name,
        chain_id=chain_id,
        poll_interval=chain_data.get('poll_interval', 30),
        rpc_nodes=rpc_nodes,
        address_filter=chain_data.get('address_filter'),
        topics_filter=chain_data.get('topics_filter'),
        apipool_server=chain_data.get('apipool_server'),
    )


def parse_cache_config(cache_data: Optional[Dict[str, Any]]) -> CacheConfig:
    """Parse cache configuration."""
    if not cache_data:
        return CacheConfig()
    
    return CacheConfig(
        max_size=cache_data.get('max_size', 10000),
    )


def parse_api_config(api_data: Optional[Dict[str, Any]]) -> APIConfig:
    """Parse API configuration."""
    if not api_data:
        return APIConfig()
    
    return APIConfig(
        host=api_data.get('host', '0.0.0.0'),
        port=api_data.get('port', 8080),
    )


def parse_log_config(log_data: Optional[Dict[str, Any]]) -> LogConfig:
    """Parse log configuration."""
    if not log_data:
        return LogConfig()
    
    return LogConfig(
        level=log_data.get('level', 'INFO'),
        format=log_data.get('format', 'json'),
        output=log_data.get('output', 'stdout'),
        file_path=log_data.get('file_path'),
    )


def parse_health_config(health_data: Optional[Dict[str, Any]]) -> HealthConfig:
    """Parse health check configuration."""
    if not health_data:
        return HealthConfig()
    
    return HealthConfig(
        enabled=health_data.get('enabled', True),
        endpoint=health_data.get('endpoint', '/health'),
    )


def parse_alert_processor_config(ap_data: Optional[Dict[str, Any]]) -> AlertProcessorConfig:
    """Parse alert processor (push) configuration."""
    if not ap_data:
        return AlertProcessorConfig()

    enabled = ap_data.get('enabled', False)
    url = ap_data.get('url', '')
    # If enabled but no URL, still create config; URL must be set for actual pushing
    return AlertProcessorConfig(
        enabled=enabled,
        url=url,
        push_interval_seconds=float(ap_data.get('push_interval_seconds', 5.0)),
        batch_size=int(ap_data.get('batch_size', 200)),
        max_payload_mb=float(ap_data.get('max_payload_mb', 10.0)),
        retry_attempts=int(ap_data.get('retry_attempts', 3)),
        retry_base_delay_sec=float(ap_data.get('retry_base_delay_sec', 1.0)),
        timeout_seconds=float(ap_data.get('timeout_seconds', 10.0)),
        reconnect_check_on_startup=ap_data.get('reconnect_check_on_startup', True),
        replay_endpoint=ap_data.get('replay_endpoint', '/ingest/logs/replay'),
    )


def parse_recorder_config(rec_data: Optional[Dict[str, Any]]) -> RecorderConfig:
    """Parse log recorder configuration."""
    if not rec_data:
        return RecorderConfig()
    return RecorderConfig(
        enabled=rec_data.get('enabled', False),
        directory=rec_data.get('directory', './recordings'),
        db_filename=rec_data.get('db_filename'),
    )


def parse_replay_config(replay_data: Optional[Dict[str, Any]]) -> ReplayConfig:
    """Parse log replay configuration."""
    if not replay_data:
        return ReplayConfig()
    return ReplayConfig(
        file_path=replay_data.get('file_path'),
        directory=replay_data.get('directory'),
        from_block=replay_data.get('from_block'),
        to_block=replay_data.get('to_block'),
        blocks_per_batch=int(replay_data.get('blocks_per_batch', 2)),
        batch_interval_seconds=float(replay_data.get('batch_interval_seconds', 5.0)),
    )


def parse_backtest_startup_config(bt_data: Optional[Dict[str, Any]]) -> BacktestStartupConfig:
    """Parse backtest startup configuration.

    When from_block and to_block are set, backtest mode will auto-start
    a task on launch for the first configured chain.
    """
    if not bt_data:
        return BacktestStartupConfig()
    return BacktestStartupConfig(
        from_block=bt_data.get('from_block'),
        to_block=bt_data.get('to_block'),
        batch_size=int(bt_data.get('batch_size', 1000)),
    )


def load_config(config_path: str) -> AppConfig:
    """Load complete application configuration."""
    config_data = load_yaml_config(config_path)
    
    chains = []
    for chain_data in config_data.get('chains', []):
        try:
            chains.append(parse_chain_config(chain_data))
        except ConfigError as e:
            raise ConfigError(f"Invalid chain configuration: {e}")
    
    if not chains:
        raise ConfigError("At least one chain must be configured")
    
    return AppConfig(
        chains=chains,
        cache=parse_cache_config(config_data.get('cache')),
        api=parse_api_config(config_data.get('api')),
        log=parse_log_config(config_data.get('log')),
        health=parse_health_config(config_data.get('health')),
        alert_processor=parse_alert_processor_config(config_data.get('alert_processor')),
        recorder=parse_recorder_config(config_data.get('recorder')),
        replay=parse_replay_config(config_data.get('replay')),
        backtest=parse_backtest_startup_config(config_data.get('backtest')),
    )
