"""Compatibility alias; implementation: pozor.presentation.cli.main."""
import sys
from importlib import import_module

from pozor.presentation.cli.main import *

sys.modules[__name__] = import_module("pozor.presentation.cli.main")
