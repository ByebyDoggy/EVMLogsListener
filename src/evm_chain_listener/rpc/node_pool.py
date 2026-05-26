"""RPC Node Pool (legacy implementation).

.. deprecated::
   This module is deprecated in favor of
   ``evm_chain_listener.rpc.apipool_client`` which uses **apipool-ng**
   ``AsyncDynamicKeyManager`` with server-driven control.

   All rotation, retry, throttling, health-tracking, and failover logic
   is now delegated to the apipool-server.  Import from ``apipool_client``
   instead.
"""

import warnings

warnings.warn(
    "evm_chain_listener.rpc.node_pool is deprecated. "
    "Use evm_chain_listener.rpc.apipool_client instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export for backward compatibility during migration period
from evm_chain_listener.rpc.apipool_client import (
    EvmRpcPool as RPCNodePool,       # type: ignore[assignment]
    EthRpcApiKey,                     # type: ignore[assignment]
    AllNodesFailedError,              # type: ignore[assignment]
)

__all__ = ["RPCNodePool", "EthRpcApiKey", "AllNodesFailedError"]
