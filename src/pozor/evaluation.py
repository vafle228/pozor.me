"""Compatibility alias; implementation: pozor.app.evaluation."""
import sys
from importlib import import_module

from pozor.app.evaluation import *

sys.modules[__name__] = import_module("pozor.app.evaluation")
