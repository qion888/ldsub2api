"""Version inspection, GitHub update, and SQLite data protection support."""

from .backup import BackupManager
from .service import RepositoryConfig, VersionControlService

__all__ = ["BackupManager", "RepositoryConfig", "VersionControlService"]
