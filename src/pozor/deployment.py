"""Compatibility alias; implementation: pozor.infra.files.deployment."""
import sys
from importlib import import_module

from pozor.infra.files.deployment import *

sys.modules[__name__] = import_module("pozor.infra.files.deployment")
