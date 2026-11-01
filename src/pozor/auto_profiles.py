"""Compatibility alias; implementation: pozor.app.auto_profiles."""
import sys
from importlib import import_module

from pozor.app.auto_profiles import *

sys.modules[__name__] = import_module("pozor.app.auto_profiles")
