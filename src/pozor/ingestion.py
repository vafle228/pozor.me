"""Compatibility alias; implementation: pozor.infra.sqlite.ingestion."""
import sys
from importlib import import_module

from pozor.infra.sqlite.ingestion import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.ingestion")
