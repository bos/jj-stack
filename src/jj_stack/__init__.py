"""JJ-native stacked GitHub review tooling."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jj-review")
except PackageNotFoundError:
    __version__ = "0.0.0"
