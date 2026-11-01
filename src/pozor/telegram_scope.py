"""Compatibility alias; implementation: pozor.core.commands.telegram_scope."""
import sys
from importlib import import_module

from pozor.core.commands.telegram_scope import *

sys.modules[__name__] = import_module("pozor.core.commands.telegram_scope")
