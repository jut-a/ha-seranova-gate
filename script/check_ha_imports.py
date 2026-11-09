"""Import every module of the integration against the installed Home Assistant.

Needs only Home Assistant itself (no test kit), so it also runs against a beta
the test kit has no build for yet. Fails when a module no longer imports
(a removed or renamed Home Assistant API) or when Home Assistant logs a
deprecation naming this integration while importing it.

    python script/check_ha_imports.py
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
import pkgutil
import sys

# Imported before the integration: Home Assistant aliases httpx on import, which
# fails when something imported httpx first.
import homeassistant  # noqa: F401
from homeassistant.const import __version__ as HA_VERSION

DOMAIN = "seranova_gate"
ROOT = Path(__file__).resolve().parents[1]


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def main() -> int:
    sys.path.insert(0, str(ROOT))
    collect = _Collect()
    logging.getLogger().addHandler(collect)

    package = importlib.import_module(f"custom_components.{DOMAIN}")
    failed: list[str] = []
    names = [f"{package.__name__}.{m.name}" for m in pkgutil.iter_modules(package.__path__)]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as err:  # report every module, not only the first
            failed.append(f"{name}: {type(err).__name__}: {err}")

    deprecations = [m for m in collect.messages if DOMAIN in m]
    print(f"Home Assistant {HA_VERSION}: imported {len(names) - len(failed)}/{len(names)} modules")
    for line in failed:
        print(f"::error::import failed - {line}")
    for line in deprecations:
        print(f"::error::deprecation - {line}")
    return 1 if failed or deprecations else 0


if __name__ == "__main__":
    sys.exit(main())
