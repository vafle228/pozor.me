"""Compatibility alias; implementation: pozor.infra.sqlite.legacy_profiles."""
import sys
from importlib import import_module

from pozor.infra.sqlite.legacy_profiles import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.legacy_profiles")
