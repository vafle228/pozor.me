"""Compatibility alias; implementation: pozor.infra.sqlite.admin_pipeline."""
import sys
from importlib import import_module

from pozor.infra.sqlite.admin_pipeline import *

sys.modules[__name__] = import_module("pozor.infra.sqlite.admin_pipeline")
