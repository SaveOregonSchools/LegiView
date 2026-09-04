"""LegiView: a durable local OLIS archive."""

from __future__ import annotations

__version__ = "0.3.0"


def create_app(config_overrides: dict | None = None):
    """Application-factory import kept lazy so the CLI has a small startup path."""
    from .web import create_app as _create_app

    return _create_app(config_overrides)


__all__ = ["__version__", "create_app"]
