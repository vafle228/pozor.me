"""Compatibility alias; implementation: pozor.infra.sqlite.import_repo."""
import sys
from importlib import import_module

from pozor.infra.sqlite.import_repo import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.import_repo")
