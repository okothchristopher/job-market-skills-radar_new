"""Source adapters.

Importing this package registers every adapter, so ``registry`` is populated by
the time the CLI resolves ``--sources global``.
"""

from __future__ import annotations

from .base import SourceAdapter, registry

# Imported for their registration side effect.
from . import (  # noqa: F401  isort:skip
    ashby,
    greenhouse,
    hn_hiring,
    kenya,
    remote_boards,
)

__all__ = ["SourceAdapter", "registry"]
