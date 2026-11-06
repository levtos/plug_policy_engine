"""Standalone Home Assistant integration for Plug Policy Engine."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from ._spec import SPEC
from . import _suggest
from .const import (
    CONF_DEVICES,
    CONF_POWER,
    CONF_SWITCH,
    DATA_ENTRIES,
    DATA_SERVICES_REGISTERED,
    DATA_WS_REGISTERED,
    DOMAIN,
    LEGACY_GLOBAL_SOURCE_MAP,
    LEGACY_POWER_SOURCE_MAP,
    MODULE_ID,
    device_dev_id_from_identifier,
    service_name,
)
from .coordinator import PlugPolicyCoordinator
from .entities import async_get_entities  # re-export
from .flow import ConfigFlowHelper, OptionsFlowHelper  # re-export
from .services_impl import SERVICES  # re-export
from .view import async_remove_view, async_setup_view
from .websocket_api import async_setup_websocket_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

__all__ = [
    "SPEC",
    "SERVICES",
    "ConfigFlowHelper",
    "OptionsFlowHelper",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_get_entities",
]


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    hass.data.setdefault(
        DOMAIN,
        {
            DATA_ENTRIES: {},
            DATA_SERVICES_REGISTERED: False,
            DATA_WS_REGISTERED: False,
        },
    )
    await _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(
        DOMAIN,
        {
            DATA_ENTRIES: {},
            DATA_SERVICES_REGISTERED: False,
            DATA_WS_REGISTERED: False,
        },
    )
    coord = PlugPolicyCoordinator(hass, entry)
    await coord.async_init()

    bucket = hass.data[DOMAIN][DATA_ENTRIES].setdefault(entry.entry_id, {})
    bucket["module_id"] = MODULE_ID
    bucket["coordinator"] = coord

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_prune_stale_devices(hass, entry, coord)
    await async_setup_view(hass)
    if not hass.data[DOMAIN].get(DATA_WS_REGISTERED):
        async_setup_websocket_api(hass)
        hass.data[DOMAIN][DATA_WS_REGISTERED] = True
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """Allow deleting plug_policy devices that are no longer configured.

    Without this, HA hides the device "Delete" button (supports_remove_device
    stayed false) and devices removed from the config lingered as
    ``unavailable`` ghosts. Returns True for stale devices (dev_id not in the
    current config); protects the hub device and currently-configured devices.
    """
    bucket = (
        hass.data.get(DOMAIN, {})
        .get(DATA_ENTRIES, {})
        .get(config_entry.entry_id, {})
    )
    coord = bucket.get("coordinator")
    configured = set(coord.configs) if coord is not None else set()
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        dev_id = device_dev_id_from_identifier(
            identifier, MODULE_ID, config_entry.entry_id
        )
        if dev_id is None:
            return False  # hub device — never removable here
        return dev_id not in configured
    return True


def _async_prune_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, coord: PlugPolicyCoordinator
) -> None:
    """Drop registry devices/entities for plug_policy devices removed from the
    config. Self-heals the ``unavailable`` ghosts left behind before
    async_remove_config_entry_device existed."""
    from homeassistant.helpers import device_registry as dr

    configured = set(coord.configs)
    dev_reg = dr.async_get(hass)
    removed = 0
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        our_ident = next(
            (ident for (domain, ident) in device.identifiers if domain == DOMAIN),
            None,
        )
        if our_ident is None:
            continue
        dev_id = device_dev_id_from_identifier(our_ident, MODULE_ID, entry.entry_id)
        if dev_id is None or dev_id in configured:
            continue  # hub device or still configured
        dev_reg.async_remove_device(device.id)  # cascades to its entities
        removed += 1
    if removed:
        _LOGGER.info(
            "Pruned %d stale plug_policy device(s) from the registry", removed
        )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate retired YAML/media-context globals and profile power sources."""
    changed = False
    data = dict(entry.data)
    options = dict(entry.options)
    for target in (data, options):
        for key, value in list(target.items()):
            if isinstance(value, str) and value in LEGACY_GLOBAL_SOURCE_MAP:
                target[key] = LEGACY_GLOBAL_SOURCE_MAP[value]
                changed = True
        if _backfill_profile_power_entities(hass, target):
            changed = True

    if changed or entry.version < 10:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=10,
        )
        _LOGGER.info("Migrated plug_policy_engine sources to Core devices/state")
    return True


def _backfill_profile_power_entities(hass: HomeAssistant, target: dict) -> bool:
    devices = target.get(CONF_DEVICES)
    if not isinstance(devices, list):
        return False

    changed = False
    new_devices: list[dict] = []
    for item in devices:
        if not isinstance(item, dict):
            new_devices.append(item)
            continue
        device = dict(item)
        power_entity = device.get(CONF_POWER)
        if isinstance(power_entity, str) and power_entity in LEGACY_POWER_SOURCE_MAP:
            device[CONF_POWER] = LEGACY_POWER_SOURCE_MAP[power_entity]
            changed = True
        switch_entity = device.get(CONF_SWITCH)
        preferred = _suggest.profile_power_entity(hass, switch_entity)
        if preferred and _power_binding_needs_backfill(
            hass,
            device.get(CONF_POWER),
            preferred,
            switch_entity,
        ):
            device[CONF_POWER] = preferred
            changed = True
        new_devices.append(device)

    if changed:
        target[CONF_DEVICES] = new_devices
    return changed


def _power_binding_needs_backfill(
    hass: HomeAssistant,
    entity_id: str | None,
    preferred: str,
    switch_entity: str | None,
) -> bool:
    if not entity_id:
        return True
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return True
    if entity_id == preferred:
        return False
    return entity_id in _suggest.profile_power_candidates(hass, switch_entity)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    bucket = hass.data.get(DOMAIN, {}).get(DATA_ENTRIES, {}).pop(entry.entry_id, None)
    coord: PlugPolicyCoordinator | None = bucket.get("coordinator") if bucket else None
    if coord is not None:
        await coord.async_shutdown()
    if not hass.data.get(DOMAIN, {}).get(DATA_ENTRIES):
        async_remove_view(hass)
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get(DATA_SERVICES_REGISTERED):
        return

    for action, sdef in SERVICES.items():
        full = service_name(MODULE_ID, action)
        if hass.services.has_service(DOMAIN, full):
            continue

        async def _handle(call: ServiceCall, _handler=sdef.handler):
            return await _handler(hass, call)

        hass.services.async_register(DOMAIN, full, _handle, schema=sdef.schema)
        _LOGGER.debug("registered service %s.%s", DOMAIN, full)

    hass.data[DOMAIN][DATA_SERVICES_REGISTERED] = True
