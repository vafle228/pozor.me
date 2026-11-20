"""Compatibility alias; implementation: pozor.infra.llm.queue."""
import sys
from importlib import import_module

from pozor.infra.llm.queue import *

sys.modules[__name__] = import_module("pozor.infra.llm.queue")
