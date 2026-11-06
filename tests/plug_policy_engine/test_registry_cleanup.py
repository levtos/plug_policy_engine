"""Regression test for the integration's device-registry cleanup."""
from __future__ import annotations

import ast
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pp_const as const
from tests.plug_policy_engine.test_module_smoke import MODULE_DIR


def test_prune_stale_devices_uses_config_entry_lookup(monkeypatch):
    from homeassistant import helpers

    entry_id = "entry-1"
    prefix = f"{const.MODULE_ID}_{entry_id}"

    def device(device_id, identifier, domain=const.DOMAIN):
        return SimpleNamespace(id=device_id, identifiers={(domain, identifier)})

    registry = SimpleNamespace(async_remove_device=Mock())
    lookup = Mock(return_value=[
        device("hub", prefix),
        device("configured", f"{prefix}_configured"),
        device("stale1", f"{prefix}_stale1"),
        device("stale2", f"{prefix}_stale2"),
        device("foreign_domain", f"{prefix}_foreign", "other"),
        device("foreign_entry", f"{const.MODULE_ID}_entry-2_foreign"),
    ])
    monkeypatch.setattr(helpers, "device_registry", SimpleNamespace(
        async_get=Mock(return_value=registry),
        async_entries_for_config_entry=lookup,
    ), raising=False)

    # Load just the setup callback under test without importing HA platforms.
    path = MODULE_DIR / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    name = "_async_prune_stale_devices"
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "PlugPolicyCoordinator": object,
        "DOMAIN": const.DOMAIN,
        "MODULE_ID": const.MODULE_ID,
        "device_dev_id_from_identifier": const.device_dev_id_from_identifier,
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)

    namespace[name](
        object(), SimpleNamespace(entry_id=entry_id),
        SimpleNamespace(configs={"configured": object()}),
    )

    lookup.assert_called_once_with(registry, entry_id)
    assert [call.args for call in registry.async_remove_device.call_args_list] == [
        ("stale1",), ("stale2",),
    ]
