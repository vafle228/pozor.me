"""Compatibility alias; implementation: pozor.infra.telegram_account."""
import sys
from importlib import import_module

from pozor.infra.telegram_account import *

sys.modules[__name__] = import_module("pozor.infra.telegram_account")
