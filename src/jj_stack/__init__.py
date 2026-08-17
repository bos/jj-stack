"""Stacked GitHub pull request tooling for jj."""

from importlib.metadata import PackageNotFoundError, version
from time import perf_counter

PROCESS_START = perf_counter()

try:
    __version__ = version("jj-stack")
except PackageNotFoundError:
    __version__ = "0.0.0"
