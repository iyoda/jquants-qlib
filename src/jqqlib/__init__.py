"""J-Quants -> Parquet -> Qlib pipeline package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jquants-qlib")
except PackageNotFoundError:  # pragma: no cover - only when the package is not installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
