"""Shared test setup.

The command-path tests need Home Assistant's test kit
(pytest-homeassistant-custom-component, which registers itself as a pytest
plugin). The decoding tests do not: they load events.py / api.py straight from
their files, so they also run where Home Assistant is not installed.

Every test also fails when Home Assistant logs a deprecation aimed at this
integration (see no_ha_deprecations): that is the warning months before a
release removes the old API, so a Dependabot bump of the test kit (or the
weekly HA beta run) goes red while the old code still works.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
from pathlib import Path
import sys
import types

import pytest

DOMAIN = "seranova_gate"
INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
_PURE_PACKAGE = "_seranova_gate_pure"

# How Home Assistant words a deprecation it traces to a custom integration:
# helpers/frame.py report_usage -> "Detected that custom integration 'seranova_gate' ...
# This will stop working in Home Assistant 2027.1 ..."; helpers/deprecation.py ->
# "The deprecated function X was called from seranova_gate. ... Use Y instead ...".
_DEPRECATION_PREFIXES = ("Detected that ", "The deprecated ")


@pytest.fixture(autouse=True)
def no_ha_deprecations(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fail the test if Home Assistant reported a deprecated use by this integration."""
    yield
    found = [
        record.getMessage()
        for when in ("setup", "call")
        for record in caplog.get_records(when)
        if record.getMessage().startswith(_DEPRECATION_PREFIXES)
        and DOMAIN in record.getMessage()
    ]
    if found:
        pytest.fail(
            "Home Assistant reported a deprecated use by this integration:\n"
            + "\n".join(f"- {message}" for message in found),
            pytrace=False,
        )


def load_pure(module: str) -> types.ModuleType:
    """Import a Home-Assistant-free module (api, const, events) without the package __init__."""
    if _PURE_PACKAGE not in sys.modules:
        package = types.ModuleType(_PURE_PACKAGE)
        package.__path__ = [str(INTEGRATION_DIR)]
        sys.modules[_PURE_PACKAGE] = package
    return importlib.import_module(f"{_PURE_PACKAGE}.{module}")
