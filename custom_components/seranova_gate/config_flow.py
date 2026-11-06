"""Config, reauth, reconfigure and options flows for Seranova Gate."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    SeranovaError,
    SeranovaWebApi,
    TwoFactorRequired,
    async_probe_system,
)
from .const import (
    CONF_APP_KEY,
    CONF_IMEI,
    CONF_POLL_MINUTES,
    CONF_SYSTEM_NAME,
    CONF_USER_CODE,
    DEFAULT_APP_KEY,
    DEFAULT_OPTIONS,
    DOMAIN,
    MAX_OUTPUTS,
    output_key,
)
from .session import async_get_session

_LOGGER = logging.getLogger(__name__)

_IMEI_RE = re.compile(r"^\d{15}$")
_MANUAL = "__manual__"

_TEXT = TextSelector()
_EMAIL = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
_CODE = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off"))

_PROBE_ERRORS = {
    "unknown_uid": ("base", "unknown_uid"),
    "bad_app_key": (CONF_APP_KEY, "invalid_app_key"),
    "bad_user_code": (CONF_USER_CODE, "invalid_user_code"),
}


def _login_error(err: Exception) -> str:
    if isinstance(err, InvalidAuth):
        return "invalid_auth"
    if isinstance(err, TwoFactorRequired):
        return "two_factor"
    if isinstance(err, CaptchaRequired):
        return "captcha"
    if isinstance(err, CannotConnect):
        return "cannot_connect"
    _LOGGER.exception("Unexpected error during SERANOVA login", exc_info=err)
    return "unknown"


class SeranovaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in, pick the system, check the access codes."""

    VERSION = 1

    def __init__(self) -> None:
        """Flow state."""
        self._email = ""
        self._password = ""
        self._token: str | None = None
        self._systems: list[dict[str, Any]] = []
        self._system: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Outputs and polling."""
        return SeranovaOptionsFlow()

    async def _login(
        self, email: str, password: str, with_systems: bool = False
    ) -> tuple[str, list[dict[str, Any]]]:
        """Log in (cookies live in the SeranovaWebApi object); optionally list the systems."""
        web = SeranovaWebApi(async_get_session(self.hass), email, password)
        token = await web.login()
        systems = await web.systems() if with_systems else []
        return token, systems

    async def _probe(self, imei: str, app_key: str, user_code: str) -> str:
        assert self._token is not None
        try:
            return await async_probe_system(
                async_get_session(self.hass), self._token, imei, app_key, user_code
            )
        except SeranovaError as err:
            _LOGGER.debug("Probe failed: %s", err)
            return "cannot_connect"

    # ---- new entry -------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """E-mail and password of the SERANOVA account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                self._token, self._systems = await self._login(
                    self._email, self._password, with_systems=True
                )
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = _login_error(err)
            else:
                self._systems = [s for s in self._systems if s.get("uid")]
                if self._systems:
                    return await self.async_step_pick()
                return await self.async_step_codes()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {vol.Required(CONF_EMAIL): _EMAIL, vol.Required(CONF_PASSWORD): _PASSWORD}
                ),
                {CONF_EMAIL: self._email},
            ),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose one of the account's systems."""
        if user_input is not None:
            choice = user_input["system"]
            self._system = next(
                (s for s in self._systems if str(s.get("uid")) == choice), None
            )
            return await self.async_step_codes()
        options = [
            SelectOptionDict(
                value=str(s["uid"]),
                label=f"{s.get('SysName') or s.get('name') or 'System'} ({s['uid']})",
            )
            for s in self._systems
        ]
        options.append(SelectOptionDict(value=_MANUAL, label="Enter the IMEI manually"))
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required("system", default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_codes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """App Key and user code, checked against the device."""
        system = self._system or {}
        known_imei = str(system["uid"]) if system.get("uid") else None
        errors: dict[str, str] = {}
        if user_input is not None:
            imei = (known_imei or user_input.get(CONF_IMEI, "")).strip()
            app_key = user_input[CONF_APP_KEY].strip()
            user_code = user_input[CONF_USER_CODE].strip()
            name = (user_input.get(CONF_SYSTEM_NAME) or "").strip() or f"PROGATE {imei[-4:]}"
            if not _IMEI_RE.match(imei):
                errors[CONF_IMEI] = "invalid_imei"
            else:
                await self.async_set_unique_id(imei)
                self._abort_if_unique_id_configured()
                outcome = await self._probe(imei, app_key, user_code)
                if outcome in _PROBE_ERRORS:
                    field, key = _PROBE_ERRORS[outcome]
                    errors[field] = key
                elif outcome == "cannot_connect":
                    errors["base"] = "cannot_connect"
                else:  # ok, offline or no reply: the codes were not refused
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_EMAIL: self._email,
                            CONF_PASSWORD: self._password,
                            CONF_IMEI: imei,
                            CONF_APP_KEY: app_key,
                            CONF_USER_CODE: user_code,
                            CONF_SYSTEM_NAME: name,
                        },
                        options=dict(DEFAULT_OPTIONS),
                    )

        schema: dict[Any, Any] = {}
        if known_imei is None:
            schema[vol.Required(CONF_IMEI)] = _TEXT
        schema[vol.Required(CONF_APP_KEY)] = _TEXT
        schema[vol.Required(CONF_USER_CODE)] = _CODE
        schema[vol.Optional(CONF_SYSTEM_NAME)] = _TEXT
        suggested = {
            CONF_APP_KEY: str(system.get("appkey") or system.get("APPKEY") or DEFAULT_APP_KEY),
            CONF_USER_CODE: str(system.get("ucode") or system.get("UCODE") or ""),
            CONF_SYSTEM_NAME: system.get("SysName") or system.get("name") or "",
        }
        if user_input:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="codes",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), suggested),
            errors=errors,
            description_placeholders={"imei": known_imei or "-"},
        )

    # ---- reauth ----------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The stored login stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._login(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            except Exception as err:  # noqa: BLE001
                errors["base"] = _login_error(err)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD}),
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    # ---- reconfigure -----------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the login, the access codes or the name."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
            app_key = user_input[CONF_APP_KEY].strip()
            user_code = (user_input.get(CONF_USER_CODE) or entry.data[CONF_USER_CODE]).strip()
            name = (user_input.get(CONF_SYSTEM_NAME) or "").strip() or entry.title
            try:
                self._token, _ = await self._login(email, password)
            except Exception as err:  # noqa: BLE001
                errors["base"] = _login_error(err)
            else:
                outcome = await self._probe(entry.data[CONF_IMEI], app_key, user_code)
                if outcome in _PROBE_ERRORS:
                    field, key = _PROBE_ERRORS[outcome]
                    errors[field] = key
                elif outcome == "cannot_connect":
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        title=name,
                        data_updates={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                            CONF_APP_KEY: app_key,
                            CONF_USER_CODE: user_code,
                            CONF_SYSTEM_NAME: name,
                        },
                    )
        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): _EMAIL,
                vol.Optional(CONF_PASSWORD): _PASSWORD,
                vol.Required(CONF_APP_KEY): _TEXT,
                vol.Optional(CONF_USER_CODE): _CODE,
                vol.Optional(CONF_SYSTEM_NAME): _TEXT,
            }
        )
        suggested = {
            CONF_EMAIL: entry.data[CONF_EMAIL],
            CONF_APP_KEY: entry.data[CONF_APP_KEY],
            CONF_SYSTEM_NAME: entry.title,
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"imei": entry.data[CONF_IMEI]},
        )


class SeranovaOptionsFlow(OptionsFlow):
    """Outputs (name, pulse length) and the status poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not any(user_input.get(output_key(n, "enabled")) for n in range(1, MAX_OUTPUTS + 1)):
                errors["base"] = "no_outputs"
            else:
                return self.async_create_entry(data=user_input)

        seconds = NumberSelector(
            NumberSelectorConfig(
                min=1, max=600, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
            )
        )
        fields: dict[Any, Any] = {
            vol.Required(CONF_POLL_MINUTES): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=240, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                )
            )
        }
        for n in range(1, MAX_OUTPUTS + 1):
            fields[vol.Required(output_key(n, "enabled"))] = BooleanSelector()
            fields[vol.Required(output_key(n, "name"))] = _TEXT
            fields[vol.Required(output_key(n, "seconds"))] = seconds
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input:
            current.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), current),
            errors=errors,
        )
