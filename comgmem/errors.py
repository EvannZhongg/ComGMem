class ComGMemError(Exception):
    """Base exception for ComGMem."""


class ConfigError(ComGMemError):
    """Raised when a memory config cannot be loaded or validated."""


class StoreError(ComGMemError):
    """Raised when the persistence layer fails."""


class IngestionNotConfiguredError(ComGMemError):
    """Raised when add() is called without a configured extraction pipeline."""
