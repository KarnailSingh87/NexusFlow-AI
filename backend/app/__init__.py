"""NexusFlow AI backend application package."""

from app.core.config import settings

__version__ = settings.app_version

__all__ = ["__version__", "settings"]
