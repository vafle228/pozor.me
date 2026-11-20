"""Compatibility alias; implementation: pozor.infra.sqlite.connection."""
import sys
from importlib import import_module

from pozor.infra.sqlite.connection import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.connection")
