"""Compatibility alias; implementation: pozor.presentation.cli.telegram_smoke."""
import sys
from importlib import import_module

from pozor.presentation.cli.telegram_smoke import *

sys.modules[__name__] = import_module("pozor.presentation.cli.telegram_smoke")
