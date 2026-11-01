"""Compatibility alias; implementation: pozor.infra.logging."""
import sys
from importlib import import_module

from pozor.infra.logging import *

sys.modules[__name__] = import_module("pozor.infra.logging")
