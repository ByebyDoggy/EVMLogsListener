"""Configuration loader for EVM Chain Listener."""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .models import (
    APIConfig,
    AppConfig,
    CacheConfig,
    ChainConfig,
    HealthConfig,
    LogConfig,
    RateLimit,
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
        raise ConfigError(f"At least one RPC node is required for chain '{name}'")
    
    return ChainConfig(
        name=name,
        chain_id=chain_id,
        poll_interval=chain_data.get('poll_interval', 30),
        rpc_nodes=rpc_nodes,
        address_filter=chain_data.get('address_filter'),
        topics_filter=chain_data.get('topics_filter'),
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
    )
