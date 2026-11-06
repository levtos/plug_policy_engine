"""Tests für die additive Tablet-Display-Policy (v0.2.0, FLEET-156) — HA-frei."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

import pp_const as C
import pp_engine as E
from tests.plug_policy_engine.test_power_sources import (
    _FakeEntry,
    _FakeHass,
    _FakeState,
    _load_coordinator_module,
)

DISPLAY = "switch.tablet_screen"


def _cfg(**overrides):
    base = dict(
        device_id="tablet1",
        name="Tablet",
        switch_entity="switch.tablet_charger",
        policy=C.POLICY_CS,
        kind=C.KIND_TABLET,
        battery_entity="sensor.tablet_battery",
    )
    base.update(overrides)
    return E.DeviceConfig(**base)


def _state(**overrides):
    s = E.DeviceState()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _ctx(**kw):
    return E.GlobalContext(**kw)


def _eval(cfg=None, state=None, ctx=None):
    return E.evaluate(cfg or _cfg(), state or _state(battery_pct=55), ctx or _ctx())


# ----- Display-Regeln -----
def test_display_on_when_home_and_awake():
    d = _eval(
        _cfg(display_entity=DISPLAY),
        _state(battery_pct=55, display_state="off"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_ON


def test_display_off_when_asleep():
    d = _eval(
        _cfg(display_entity=DISPLAY),
        _state(battery_pct=55, display_state="on"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_SLEEP),
    )
    assert d.desired_display_state == C.DESIRED_OFF


def test_display_off_when_truly_away():
    d = _eval(
        _cfg(display_entity=DISPLAY),
        _state(battery_pct=55),
        _ctx(presence=C.PRESENCE_AWAY, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_OFF


def test_display_on_when_bei_eltern_and_awake():
    # bei_eltern zählt als zuhause-artig → kein Lock.
    d = _eval(
        _cfg(display_entity=DISPLAY),
        _state(battery_pct=55),
        _ctx(presence=C.PRESENCE_AT_PARENTS, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_ON


def test_display_keep_when_presence_unknown():
    d = _eval(
        _cfg(display_entity=DISPLAY),
        _state(battery_pct=55),
        _ctx(presence=None, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_KEEP


def test_display_keep_when_no_display_entity():
    d = _eval(
        _cfg(display_entity=None),
        _state(battery_pct=55),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_KEEP


# ----- Lade-Policy bleibt unberührt -----
def test_charging_logic_unaffected_low_battery_charges():
    d = _eval(
        _cfg(display_entity=DISPLAY, tablet_low=40, tablet_high=80),
        _state(battery_pct=30, switch_state="off"),
        _ctx(presence=C.PRESENCE_AWAY, bio=C.BIO_SLEEP),
    )
    assert d.desired_switch_state == C.DESIRED_ON      # Laden trotz away/sleep
    assert d.desired_display_state == C.DESIRED_OFF    # Display aber aus


def test_charging_stop_high_battery():
    d = _eval(
        _cfg(display_entity=DISPLAY, tablet_low=40, tablet_high=80),
        _state(battery_pct=85, switch_state="on"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE),
    )
    assert d.desired_switch_state == C.DESIRED_OFF     # Ladeschluss
    assert d.desired_display_state == C.DESIRED_ON     # Display an (home+awake)


# ----- Default für Nicht-Tablet -----
def test_non_tablet_display_defaults_keep():
    d = E.evaluate(
        E.DeviceConfig(device_id="x", name="x", switch_entity="switch.x", kind=C.KIND_GENERIC),
        _state(switch_state="on"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE),
    )
    assert d.desired_display_state == C.DESIRED_KEEP


SCREEN = "light.tablet_screen"
BIO = "sensor.bio"


@pytest.fixture
def display_coordinator(monkeypatch):
    mod = _load_coordinator_module()
    states = {
        "sensor.presence": _FakeState(C.PRESENCE_HOME),
        BIO: _FakeState(C.BIO_AWAKE),
        "sensor.tablet_battery": _FakeState("55"),
        "switch.tablet_charger": _FakeState("on"),
    }
    hass = _FakeHass(states)
    hass.is_running = True
    entry = _FakeEntry({
        C.CONF_ENABLE_CONTROL: True,
        C.CONF_PRESENCE: "sensor.presence",
        C.CONF_BIO: BIO,
        C.CONF_DEVICES: [{
            "device_id": "tablet",
            C.CONF_SWITCH: "switch.tablet_charger",
            C.CONF_BATTERY: "sensor.tablet_battery",
            C.CONF_KIND: C.KIND_TABLET,
            C.CONF_DISPLAY_ENTITY: SCREEN,
        }],
    })
    coord = mod.PlugPolicyCoordinator(hass, entry)
    clock = [0.0]
    monkeypatch.setattr(mod, "monotonic", lambda: clock[0])
    return mod, coord, states, clock


@pytest.mark.parametrize(
    ("unavailable", "bio", "recovered_state", "service"),
    [
        ("unavailable", C.BIO_SLEEP, "on", "turn_off"),
        (None, C.BIO_AWAKE, "off", "turn_on"),
        ("unknown", C.BIO_AWAKE, "off", "turn_on"),
    ],
)
def test_display_reconciles_current_intent_on_recovery(
    display_coordinator, monkeypatch, caplog, unavailable, bio, recovered_state, service,
):
    mod, coord, states, clock = display_coordinator
    states[SCREEN] = None if unavailable is None else _FakeState(unavailable)
    states[BIO] = _FakeState(C.BIO_AWAKE if bio == C.BIO_SLEEP else C.BIO_SLEEP)
    listeners = {}
    tasks = []

    def track_state_changes(_hass, entity_ids, listener):
        listeners.update(dict.fromkeys(entity_ids, listener))
        return lambda: None

    monkeypatch.setattr(mod, "async_track_state_change_event", track_state_changes)
    coord.hass.async_create_task = lambda coro: tasks.append(asyncio.create_task(coro))

    async def run():
        await coord.async_init()
        clock[0] = 20
        states[BIO] = _FakeState(bio)
        listeners[BIO](None)
        await tasks.pop()
        assert coord.hass.services.calls == []

        states[SCREEN] = _FakeState(recovered_state)
        listeners[SCREEN](None)
        await tasks.pop()
        assert len(coord.hass.services.calls) == 1
        call = coord.hass.services.calls[0]
        assert (call["domain"], call["service"], call["target"]) == (
            "homeassistant", service, {"entity_id": SCREEN},
        )
        assert not caplog.records

    asyncio.run(run())


def test_display_warns_once_per_persistent_outage(display_coordinator, caplog):
    _, coord, states, clock = display_coordinator
    states[SCREEN] = _FakeState("unavailable")

    async def run():
        await coord.async_evaluate_all()
        clock[0] = 59
        await coord.async_evaluate_all()
        assert not caplog.records

        clock[0] = 60
        await coord.async_evaluate_all()
        clock[0] = 120
        await coord.async_evaluate_all()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert SCREEN in caplog.records[0].message

        states[SCREEN] = _FakeState("on")
        await coord.async_evaluate_all()
        del states[SCREEN]
        await coord.async_evaluate_all()
        assert len(caplog.records) == 1
        clock[0] = 180
        await coord.async_evaluate_all()
        assert len(caplog.records) == 2
        assert coord.hass.services.calls == []

    asyncio.run(run())


def test_display_checks_availability_after_charger_apply(display_coordinator):
    _, coord, states, _ = display_coordinator
    states[SCREEN] = _FakeState("off")

    async def drop_display(*_args):
        states[SCREEN] = _FakeState("unavailable")

    coord._apply_decision = AsyncMock(side_effect=drop_display)
    asyncio.run(coord.async_evaluate_all())

    coord._apply_decision.assert_awaited_once()
    assert coord.hass.services.calls == []
