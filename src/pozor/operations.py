"""Compatibility alias; implementation: pozor.infra.sqlite.operations."""
import sys
from importlib import import_module

from pozor.infra.sqlite.operations import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.operations")
