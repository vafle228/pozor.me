"""Compatibility alias; implementation: pozor.app.telegram_runtime."""
import sys
from importlib import import_module

from pozor.app.telegram_runtime import *

sys.modules[__name__] = import_module("pozor.app.telegram_runtime")
