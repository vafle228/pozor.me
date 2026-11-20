"""Compatibility alias; implementation: pozor.app.doctor."""
import sys
from importlib import import_module

from pozor.app.doctor import *

sys.modules[__name__] = import_module("pozor.app.doctor")
