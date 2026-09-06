"""Source adapters.

Importing this package registers every adapter, so ``registry`` is populated by
the time the CLI resolves ``--sources global``.
"""

from __future__ import annotations

from .base import SourceAdapter, registry

# Imported for their registration side effect.
from . import ashby, greenhouse, hn_hiring, remote_boards  # noqa: F401  isort:skip

__all__ = ["SourceAdapter", "registry"]
