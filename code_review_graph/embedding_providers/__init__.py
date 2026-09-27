"""Embedding profiles and local model backends.

Only profile resolution is imported here; backend modules (and the model
packages behind them) load on first use.
"""

from .profiles import (
    PROFILE_BACKENDS,
    BackendSpec,
    Resolution,
    module_available,
    platform_key,
    resolve_profile,
)

__all__ = [
    "PROFILE_BACKENDS",
    "BackendSpec",
    "Resolution",
    "module_available",
    "platform_key",
    "resolve_profile",
]
