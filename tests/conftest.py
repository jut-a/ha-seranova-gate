"""Shared test setup.

The command-path tests need Home Assistant's test kit
(pytest-homeassistant-custom-component, which registers itself as a pytest
plugin). The decoding tests do not: they load events.py / api.py straight from
their files, so they also run where Home Assistant is not installed.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "seranova_gate"
_PURE_PACKAGE = "_seranova_gate_pure"


def load_pure(module: str) -> types.ModuleType:
    """Import a Home-Assistant-free module (api, const, events) without the package __init__."""
    if _PURE_PACKAGE not in sys.modules:
        package = types.ModuleType(_PURE_PACKAGE)
        package.__path__ = [str(INTEGRATION_DIR)]
        sys.modules[_PURE_PACKAGE] = package
    return importlib.import_module(f"{_PURE_PACKAGE}.{module}")
