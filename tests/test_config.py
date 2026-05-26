"""Tests for config module."""

import os
import pytest
from pathlib import Path

from src.evm_chain_listener.config import (
    load_config,
    expand_env_vars,
    ConfigError,
)


class TestExpandEnvVars:
    """Test environment variable expansion."""
    
    def test_expand_simple_var(self):
        """Test expanding a simple environment variable."""
        os.environ["TEST_VAR"] = "test_value"
        
        result = expand_env_vars("${TEST_VAR}")
        
        assert result == "test_value"
    
    def test_expand_multiple_vars(self):
        """Test expanding multiple environment variables."""
        os.environ["VAR1"] = "value1"
        os.environ["VAR2"] = "value2"
        
        result = expand_env_vars("${VAR1}/${VAR2}/path")
        
        assert result == "value1/value2/path"
    
    def test_expand_missing_var(self):
        """Test that missing variables are kept as-is."""
        result = expand_env_vars("${NONEXISTENT}")
        
        assert result == "${NONEXISTENT}"
    
    def test_expand_in_dict(self):
        """Test expanding variables in dictionary."""
        os.environ["HOST"] = "localhost"
        
        input_dict = {
            "url": "https://${HOST}:8080",
            "other": "value",
        }
        
        result = expand_env_vars(input_dict)
        
        assert result["url"] == "https://localhost:8080"
        assert result["other"] == "value"
    
    def test_expand_in_list(self):
        """Test expanding variables in list."""
        os.environ["ITEM"] = "expanded"
        
        result = expand_env_vars(["${ITEM}", "static"])
        
        assert result == ["expanded", "static"]


class TestLoadConfig:
    """Test configuration loading."""
    
    def test_load_valid_config(self, tmp_path):
        """Test loading a valid configuration file."""
        config_content = """
chains:
  - name: ethereum
    chain_id: 1
    poll_interval: 30
    rpc_nodes:
      - url: "https://eth.example.com"
        priority: 1
        type: public
        rate_limit:
          requests_per_second: 10
          burst: 20

cache:
  max_size: 5000

api:
  host: "0.0.0.0"
  port: 9090

log:
  level: DEBUG
  format: json
  output: stdout

health:
  enabled: true
  endpoint: /health
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)
        
        config = load_config(str(config_file))
        
        assert len(config.chains) == 1
        assert config.chains[0].name == "ethereum"
        assert config.chains[0].chain_id == 1
        assert config.chains[0].poll_interval == 30
        assert len(config.chains[0].rpc_nodes) == 1
        assert config.chains[0].rpc_nodes[0].url == "https://eth.example.com"
        assert config.cache.max_size == 5000
        assert config.api.port == 9090
        assert config.log.level == "DEBUG"
    
    def test_load_config_missing_file(self):
        """Test that missing config file raises error."""
        with pytest.raises(ConfigError, match="Configuration file not found"):
            load_config("/nonexistent/path/config.yaml")
    
    def test_load_config_missing_chain(self, tmp_path):
        """Test that config without chains raises error."""
        config_content = """
cache:
  max_size: 1000
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)
        
        with pytest.raises(ConfigError, match="At least one chain must be configured"):
            load_config(str(config_file))
    
    def test_load_config_missing_rpc_node(self, tmp_path):
        """Test that chain without RPC node raises error."""
        config_content = """
chains:
  - name: ethereum
    chain_id: 1
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)
        
        with pytest.raises(ConfigError, match="At least one RPC node or apipool_server is required"):
            load_config(str(config_file))
