# SPDX-License-Identifier: AGPL-3.0
"""Text-only, independently installable OpenViking runtime."""

__version__ = "0.1.0"

from .client import Client

__all__ = ["Client", "__version__"]
