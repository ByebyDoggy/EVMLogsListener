"""RPC exceptions -- backward-compatible re-exports from apipool_client.

.. deprecated::
   Import exceptions directly from
   ``evm_chain_listener.rpc.apipool_client`` instead.
"""

import warnings

warnings.warn(
    "Import from 'evm_chain_listener.rpc.exceptions' is deprecated; "
    "import directly from 'evm_chain_listener.rpc.apipool_client'.",
    DeprecationWarning,
    stacklevel=2,
)

from evm_chain_listener.rpc.apipool_client import (
    AllNodesFailedError,
    InvalidResponseError,
    LogLimitExceededError,
    RpcRateLimitError as RateLimitError,
    RpcTimeoutError as TimeoutError,
    parse_rpc_error,
)

__all__ = [
    "AllNodesFailedError",
    "InvalidResponseError",
    "LogLimitExceededError",
    "RateLimitError",
    "TimeoutError",
    "parse_rpc_error",
]
