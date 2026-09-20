"""Coordinator für Plug Policy Engine.

Liest HA-State, ruft die reine `engine.evaluate(...)`-Funktion und (optional)
treibt Switches. Cross-Modul-Inputs (Context/Media/Wake/Title-Classifier)
kommen ausschließlich als HA-Entity-IDs aus der Konfig — kein Python-Import
anderer Toolbox-Module.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACTIVE_THRESHOLD,
    CONF_ACTIVITY,
    CONF_ALLOWED_CONTEXTS,
    CONF_BATTERY,
    CONF_BIO,
    CONF_DAY,
    CONF_DEADBAND_HIGH,
    CONF_DEADBAND_LOW,
    CONF_DEVICES,
    CONF_DISPLAY_ENTITY,
    CONF_DIFFUSER_OFF_MIN,
    CONF_DIFFUSER_ON_MIN,
    CONF_ENABLE_CONTROL,
    CONF_ENTERTAINMENT,
    CONF_GAMING_SOURCE,
    CONF_TV_ACTIVE,
    CONF_IDLE_THRESHOLD,
    CONF_KIND,
    CONF_MANUAL_COOLDOWN,
    CONF_MEDIA,
    CONF_NAME,
    CONF_NEVER_CUT_ACTIVE,
    CONF_POLICY,
    CONF_POWER,
    CONF_PRESENCE,
    CONF_SCAN_INTERVAL,
    CONF_STABLE_OFF,
    CONF_SWITCH,
    CONF_TABLET_HIGH,
    CONF_TABLET_LOW,
    CONF_UNKNOWN,
    CONF_WAKE_SIGNAL_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DESIRED_KEEP,
    DESIRED_OFF,
    DESIRED_ON,
    DATA_ENTRIES,
    DOMAIN,
    GLOBAL_PREFILL,
    KIND_PC,
    MODULE_ID,
    STORAGE_VERSION,
)
from . import _suggest
from .apply_guard import (
    MIN_COMMAND_INTERVAL_SECONDS,
    allows_auto_suspend_for_reassert,
    debounce_suppresses,
    record_reassert_and_should_suspend,
    service_target_state_available,
)
from .engine import Decision, DeviceConfig, DeviceState, GlobalContext, evaluate
from .storage import make_store

_LOGGER = logging.getLogger(__name__)

_PROFILE_POWER_BY_SWITCH = {
    "switch.living_pc_plug": ("sensor.benni_master_pc",),
    "switch.living_denon_plug_denon": ("sensor.benni_master_denon",),
    "switch.living_ps5_plug": ("sensor.benni_master_ps5",),
    "switch.living_switch_plug": ("sensor.benni_master_switch",),
    "switch.wohnbereich_steckdose_tv": ("sensor.benni_master_tv",),
    "switch.kitchen_washing_machine_plug": ("sensor.benni_master_household_plug",),
    "switch.kitchen_dryer_plug": ("sensor.benni_master_household_plug",),
    "switch.kitchen_dishwasher_plug": ("sensor.benni_master_household_plug",),
    "switch.kitchen_diffuser_plug": ("sensor.benni_master_household_plug",),
}

_ACTION_RETRY_SECONDS = 10.0
# Optional per-entity overrides for the command de-bounce window. Every switch
# is de-bounced by MIN_COMMAND_INTERVAL_SECONDS by default (see apply_guard);
# entries here only override that default for a specific entity.
_ENTITY_COMMAND_COOLDOWN_SECONDS: dict[str, float] = {}


class PlugPolicyCoordinator:
    """Liest HA-State, ruft die reine Engine, exposes Decisions, treibt Switches optional."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store = make_store(
            hass, MODULE_ID, f"state_{entry.entry_id}", version=STORAGE_VERSION
        )
        self._unsub: list = []
        self._unsub_started = None
        self._listeners: list = []
        self._ha_started = False

        data = {**entry.data, **entry.options}
        self.enable_control: bool = bool(data.get(CONF_ENABLE_CONTROL, False))
        self.scan_interval: int = int(data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        self.global_entities = {
            "presence": self._global_entity(data, CONF_PRESENCE),
            "bio": self._global_entity(data, CONF_BIO),
            "day": self._global_entity(data, CONF_DAY),
            "media": self._global_entity(data, CONF_MEDIA),
            "gaming_source": self._global_entity(data, CONF_GAMING_SOURCE),
            "entertainment": self._global_entity(data, CONF_ENTERTAINMENT),
            "activity": self._global_entity(data, CONF_ACTIVITY),
            "tv_active": self._global_entity(data, CONF_TV_ACTIVE),
        }

        self.configs: dict[str, DeviceConfig] = {}
        for d in data.get(CONF_DEVICES, []):
            cfg = DeviceConfig(
                device_id=d["device_id"],
                name=d.get(CONF_NAME, d["device_id"]),
                switch_entity=d[CONF_SWITCH],
                power_entity=d.get(CONF_POWER)
                or _suggest.profile_power_entity(hass, d.get(CONF_SWITCH)),
                battery_entity=d.get(CONF_BATTERY),
                display_entity=d.get(CONF_DISPLAY_ENTITY),
                policy=d.get(CONF_POLICY, "HB"),
                kind=d.get(CONF_KIND, "generic"),
                active_threshold=float(d.get(CONF_ACTIVE_THRESHOLD, 5.0)),
                idle_threshold=float(d.get(CONF_IDLE_THRESHOLD, 2.0)),
                deadband_lower=d.get(CONF_DEADBAND_LOW),
                deadband_upper=d.get(CONF_DEADBAND_HIGH),
                stable_off_seconds=int(d.get(CONF_STABLE_OFF, 600)),
                unknown_behavior=d.get(CONF_UNKNOWN, "assume_active"),
                allowed_contexts=list(d.get(CONF_ALLOWED_CONTEXTS, [])),
                never_cut_when_active=bool(d.get(CONF_NEVER_CUT_ACTIVE, True)),
                wake_signal_only=bool(d.get(CONF_WAKE_SIGNAL_ONLY, False)),
                tablet_low=int(d.get(CONF_TABLET_LOW, 40)),
                tablet_high=int(d.get(CONF_TABLET_HIGH, 80)),
                diffuser_on_minutes=int(d.get(CONF_DIFFUSER_ON_MIN, 15)),
                diffuser_off_minutes=int(d.get(CONF_DIFFUSER_OFF_MIN, 15)),
                manual_on_cooldown_seconds=int(d.get(CONF_MANUAL_COOLDOWN, 900)),
            )
            self.configs[cfg.device_id] = cfg

        self.states: dict[str, DeviceState] = {dev_id: DeviceState() for dev_id in self.configs}
        self.decisions: dict[str, Decision] = {}
        self.last_action: dict[str, dict[str, Any]] = {}
        self._pending_actions: dict[str, dict[str, Any]] = {}
        # Last command actually *sent* per switch entity, as (service, ts).
        # Drives the de-bounce; deliberately NOT cleared when the target state
        # is reached, so a flapping/non-latching plug cannot trigger an
        # immediate re-assert (FLEET-107).
        self._last_command_by_entity: dict[str, tuple[str, float]] = {}
        self._last_cooldown_log_ts_by_entity: dict[str, float] = {}
        self._reassert_history_by_entity: dict[str, list[tuple[str, float]]] = {}
        self._auto_suspended_by_device: dict[str, dict[str, Any]] = {}
        self._display_unavailable_log_ts_by_entity: dict[str, float] = {}
        self.last_context: dict[str, Any] = {}
        self.last_update_ts: float | None = None

    def _global_entity(self, data: dict[str, Any], key: str) -> str | None:
        configured = data.get(key)
        if configured:
            state = self.hass.states.get(configured)
            if state is not None and state.state not in ("unknown", "unavailable"):
                return configured
        return GLOBAL_PREFILL.get(key) or configured

    # ---------- lifecycle ----------
    async def async_init(self) -> None:
        stored = await self._store.async_load() or {}
        for dev_id, persisted in (stored.get("devices") or {}).items():
            if dev_id in self.states:
                st = self.states[dev_id]
                st.manual_on_until_ts = persisted.get("manual_on_until_ts")
                st.last_idle_since_ts = persisted.get("last_idle_since_ts")
                st.diffuser_phase = persisted.get("diffuser_phase", "off")
                st.diffuser_phase_since_ts = persisted.get("diffuser_phase_since_ts", 0.0)
                st.suspended = persisted.get("suspended", False)
                self.last_action[dev_id] = persisted.get("last_action", {})

        if self.hass.is_running:
            self._ha_started = True
        else:
            self._unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_started
            )
            self._unsub.append(self._unsub_started)

        watch = set()
        for v in self.global_entities.values():
            if v:
                watch.add(v)
        for cfg in self.configs.values():
            watch.add(cfg.switch_entity)
            if cfg.power_entity:
                watch.add(cfg.power_entity)
            if cfg.battery_entity:
                watch.add(cfg.battery_entity)
            if cfg.display_entity:
                watch.add(cfg.display_entity)

        if watch:
            self._unsub.append(
                async_track_state_change_event(self.hass, list(watch), self._on_state_change)
            )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._on_interval, timedelta(seconds=self.scan_interval)
            )
        )
        await self.async_evaluate_all()

    async def async_shutdown(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()
        await self._async_save()

    @callback
    def _on_started(self, _event) -> None:
        self._ha_started = True
        if self._unsub_started in self._unsub:
            self._unsub.remove(self._unsub_started)
        self._unsub_started = None
        self.hass.async_create_task(self.async_evaluate_all(ha_just_started=True))

    @callback
    def _on_state_change(self, _event) -> None:
        self.hass.async_create_task(self.async_evaluate_all())

    @callback
    def _on_interval(self, _now) -> None:
        self.hass.async_create_task(self.async_evaluate_all())

    # ---------- read HA state into engine inputs ----------
    def _read_str(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        s = self.hass.states.get(entity_id)
        return s.state if s else None

    def _facade_attr_names(self, cfg: DeviceConfig | None, suffix: str) -> tuple[str, ...]:
        if cfg is None:
            return ()
        names: list[str] = []
        for base in (cfg.device_id, _suggest.base_slug(cfg.switch_entity)):
            if base and f"{base}_{suffix}" not in names:
                names.append(f"{base}_{suffix}")
        return tuple(names)

    def _read_power(self, entity_id: str | None, cfg: DeviceConfig | None = None) -> Any:
        if not entity_id:
            return None
        s = self.hass.states.get(entity_id)
        if s is None:
            return None
        for attr in self._facade_attr_names(cfg, "watt"):
            value = s.attributes.get(attr)
            if _safe_float(value) is not None:
                return value
        for attr in self._facade_attr_names(cfg, "power_w"):
            value = s.attributes.get(attr)
            if _safe_float(value) is not None:
                return value
        if _safe_float(s.state) is not None:
            return s.state
        for attr in ("watt", "power_w", "power"):
            value = s.attributes.get(attr)
            if _safe_float(value) is not None:
                return value
        return s.state

    def _read_active_hint(self, entity_id: str | None, cfg: DeviceConfig | None = None) -> Any:
        if not entity_id:
            return None
        s = self.hass.states.get(entity_id)
        if s is None:
            return None
        for attr in self._facade_attr_names(cfg, "active"):
            value = s.attributes.get(attr)
            if isinstance(value, bool):
                return "active" if value else "idle"
            if isinstance(value, str) and value.lower() in ("true", "false", "on", "off"):
                return "active" if value.lower() in ("true", "on") else "idle"
        for attr in ("is_active", "powered", "watt_active", "protection_relevant"):
            value = s.attributes.get(attr)
            if isinstance(value, bool):
                return "active" if value else "idle"
            if isinstance(value, str) and value.lower() in ("true", "false", "on", "off"):
                return "active" if value.lower() in ("true", "on") else "idle"
        return None

    def _read_bool(self, entity_id: str | None) -> bool | None:
        s = self._read_str(entity_id)
        if s is None:
            return None
        return s.lower() in ("on", "true", "1", "active", "playing")

    def _read_tv_active(self, entity_id: str | None) -> bool | None:
        """Read the authoritative TV-master truth for the bias-light policy.

        ``off``/``standby`` → False; ``unknown``/``unavailable``/``degraded``/
        ungebunden → None (hold, no command during master dropouts);
        alles andere (``active``/``on``/``playing``) → True.
        """
        s = self._read_str(entity_id)
        if s is None:
            return None
        v = s.lower()
        if v in ("unknown", "unavailable", "degraded"):
            return None
        return v not in ("off", "standby")

    def _resolve_power_entity(self, cfg: DeviceConfig) -> str | None:
        if cfg.power_entity and self.hass.states.get(cfg.power_entity) is not None:
            return cfg.power_entity
        for entity_id in _PROFILE_POWER_BY_SWITCH.get(cfg.switch_entity, ()):
            if self.hass.states.get(entity_id) is not None:
                cfg.power_entity = entity_id
                return entity_id
        cfg.power_entity = _suggest.profile_power_entity(self.hass, cfg.switch_entity)
        return cfg.power_entity

    def _build_context(self) -> GlobalContext:
        return GlobalContext(
            presence=self._read_str(self.global_entities["presence"]),
            bio=self._read_str(self.global_entities["bio"]),
            day_phase=self._read_str(self.global_entities["day"]),
            media_context=self._read_str(self.global_entities["media"]),
            gaming_source=self._read_str(self.global_entities["gaming_source"]),
            entertainment_active=self._read_bool(self.global_entities["entertainment"]),
            activity=self._read_str(self.global_entities["activity"]),
            tv_active=self._read_tv_active(self.global_entities["tv_active"]),
            now_ts=dt_util.utcnow().timestamp(),
        )

    def _refresh_device_state(self, cfg: DeviceConfig) -> DeviceState:
        st = self.states[cfg.device_id]
        previous_switch_state = st.switch_state
        st.switch_state = self._read_str(cfg.switch_entity)
        if (
            cfg.kind == KIND_PC
            and previous_switch_state == "off"
            and st.switch_state == "on"
        ):
            st.manual_on_until_ts = (
                dt_util.utcnow().timestamp() + cfg.manual_on_cooldown_seconds
            )
        self._clear_pending_action_if_reached(cfg.device_id, st.switch_state)
        power_entity = self._resolve_power_entity(cfg)
        st.power_w = self._read_power(power_entity, cfg)
        st.active_hint = self._read_active_hint(power_entity, cfg)
        st.battery_pct = self._read_str(cfg.battery_entity) if cfg.battery_entity else None
        st.display_state = self._read_str(cfg.display_entity) if cfg.display_entity else None
        return st

    # ---------- evaluation + actions ----------
    async def async_evaluate_all(self, *, ha_just_started: bool = False) -> None:
        ctx = self._build_context()
        self.last_update_ts = ctx.now_ts
        self.last_context = {
            "presence": ctx.presence,
            "bio": ctx.bio,
            "day_phase": ctx.day_phase,
            "media_context": ctx.media_context,
            "gaming_source": ctx.gaming_source,
            "entertainment_active": ctx.entertainment_active,
            "activity": ctx.activity,
            "tv_active": ctx.tv_active,
        }
        for cfg in self.configs.values():
            await self._async_evaluate_one(cfg, ctx, ha_just_started=ha_just_started)

        await self._async_save()
        for cb in self._listeners:
            cb()

    async def _async_evaluate_one(
        self,
        cfg: DeviceConfig,
        ctx: GlobalContext,
        *,
        ha_just_started: bool = False,
    ) -> None:
        st = self._refresh_device_state(cfg)
        self._resume_auto_suspended_if_stable(cfg, st, ctx.now_ts)
        decision = evaluate(cfg, st, ctx, ha_just_started=ha_just_started)
        self.decisions[cfg.device_id] = decision
        if decision.active_state == "idle":
            if st.last_idle_since_ts is None:
                st.last_idle_since_ts = ctx.now_ts
        else:
            st.last_idle_since_ts = None

        if cfg.kind == "diffuser" and decision.desired_switch_state in (DESIRED_ON, DESIRED_OFF):
            new_phase = "on" if decision.desired_switch_state == DESIRED_ON else "off"
            if st.diffuser_phase != new_phase:
                st.diffuser_phase = new_phase
                st.diffuser_phase_since_ts = ctx.now_ts

        if self.enable_control:
            await self._apply_decision(cfg, st, decision)
            await self._apply_display(cfg, st, decision)

    def _resume_auto_suspended_if_stable(
        self,
        cfg: DeviceConfig,
        st: DeviceState,
        now_ts: float,
    ) -> None:
        auto = self._auto_suspended_by_device.get(cfg.device_id)
        if not st.suspended or not auto:
            return
        target_state = auto.get("state")
        sent_ts = float(auto.get("ts") or 0.0)
        if (
            st.switch_state
            and st.switch_state.lower() == target_state
            and now_ts - sent_ts >= MIN_COMMAND_INTERVAL_SECONDS
        ):
            st.suspended = False
            self._auto_suspended_by_device.pop(cfg.device_id, None)
            self._reassert_history_by_entity.pop(cfg.switch_entity, None)
            _LOGGER.info(
                "plug_policy_engine: resumed %s after %s stayed %s for %.0fs",
                cfg.device_id,
                cfg.switch_entity,
                target_state,
                MIN_COMMAND_INTERVAL_SECONDS,
            )

    async def _apply_decision(self, cfg: DeviceConfig, st: DeviceState, dec: Decision) -> None:
        if dec.desired_switch_state == DESIRED_KEEP:
            self._pending_actions.pop(cfg.device_id, None)
            return
        target = "turn_on" if dec.desired_switch_state == DESIRED_ON else "turn_off"
        target_state = "on" if dec.desired_switch_state == DESIRED_ON else "off"
        current = (st.switch_state or "").lower()
        if current == target_state:
            self._pending_actions.pop(cfg.device_id, None)
            return

        now_ts = dt_util.utcnow().timestamp()
        cooldown = _ENTITY_COMMAND_COOLDOWN_SECONDS.get(
            cfg.switch_entity, MIN_COMMAND_INTERVAL_SECONDS
        )
        if debounce_suppresses(
            self._last_command_by_entity.get(cfg.switch_entity),
            target,
            now_ts,
            cooldown,
        ):
            last_log_ts = self._last_cooldown_log_ts_by_entity.get(cfg.switch_entity)
            if last_log_ts is None or now_ts - last_log_ts >= cooldown:
                self._last_cooldown_log_ts_by_entity[cfg.switch_entity] = now_ts
                _LOGGER.debug(
                    "plug_policy_engine: skipped %s on %s due to %.0fs command cooldown "
                    "(non-latching plug re-assert)",
                    target,
                    cfg.switch_entity,
                    cooldown,
                )
            return

        pending = self._pending_actions.get(cfg.device_id)
        if (
            pending
            and pending.get("service") == target
            and now_ts - float(pending.get("ts") or 0.0) < _ACTION_RETRY_SECONDS
        ):
            return
        try:
            await self.hass.services.async_call(
                "switch", target, {}, blocking=False, target={"entity_id": cfg.switch_entity},
            )
            self._pending_actions[cfg.device_id] = {
                "service": target,
                "state": target_state,
                "ts": now_ts,
            }
            self._last_command_by_entity[cfg.switch_entity] = (target, now_ts)
            history, should_suspend = record_reassert_and_should_suspend(
                self._reassert_history_by_entity.get(cfg.switch_entity, ()),
                target,
                now_ts,
            )
            self._reassert_history_by_entity[cfg.switch_entity] = history
            if should_suspend and allows_auto_suspend_for_reassert(
                kind=cfg.kind,
                target_service=target,
                battery_pct=st.battery_pct,
                tablet_low=cfg.tablet_low,
            ):
                st.suspended = True
                self._auto_suspended_by_device[cfg.device_id] = {
                    "service": target,
                    "state": target_state,
                    "ts": now_ts,
                }
                _LOGGER.warning(
                    "plug_policy_engine: auto-suspended %s (%s) after %d repeated "
                    "%s commands; plug appears non-latching",
                    cfg.device_id,
                    cfg.switch_entity,
                    len(history),
                    target,
                )
            elif should_suspend:
                _LOGGER.warning(
                    "plug_policy_engine: kept %s active despite repeated %s commands; "
                    "tablet charging fail-safe is active",
                    cfg.device_id,
                    target,
                )
            self.last_action[cfg.device_id] = {
                "action": target,
                "reason": dec.reason,
                "ts": dt_util.utcnow().isoformat(),
            }
            _LOGGER.info(
                "plug_policy_engine: %s on %s — %s", target, cfg.switch_entity, dec.reason,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "plug_policy_engine: switch call failed for %s: %s", cfg.switch_entity, err,
            )

    async def _apply_display(self, cfg: DeviceConfig, st: DeviceState, dec: Decision) -> None:
        """Tablet-Screen schalten (additiv, gated). Domain-agnostisch via
        ``homeassistant.turn_on/off`` (display_entity kann switch/light/input_boolean
        sein). Idempotent: nur schalten, wenn der Ist-Zustand abweicht."""
        if not cfg.display_entity:
            return
        if dec.desired_display_state not in (DESIRED_ON, DESIRED_OFF):
            return
        target_state = "on" if dec.desired_display_state == DESIRED_ON else "off"
        current = (st.display_state or "").lower()
        if not service_target_state_available(current):
            now_ts = dt_util.utcnow().timestamp()
            last_log_ts = self._display_unavailable_log_ts_by_entity.get(cfg.display_entity)
            if last_log_ts is None or now_ts - last_log_ts >= MIN_COMMAND_INTERVAL_SECONDS:
                self._display_unavailable_log_ts_by_entity[cfg.display_entity] = now_ts
                _LOGGER.warning(
                    "plug_policy_engine: skipped display service for %s; current state is %r",
                    cfg.display_entity,
                    st.display_state,
                )
            return
        if current == target_state:
            return
        service = "turn_on" if dec.desired_display_state == DESIRED_ON else "turn_off"
        try:
            await self.hass.services.async_call(
                "homeassistant", service, {}, blocking=False,
                target={"entity_id": cfg.display_entity},
            )
            _LOGGER.info(
                "plug_policy_engine: display %s on %s — %s",
                service, cfg.display_entity, dec.reason,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "plug_policy_engine: display call failed for %s: %s",
                cfg.display_entity, err,
            )

    def _clear_pending_action_if_reached(self, device_id: str, switch_state: str | None) -> None:
        pending = self._pending_actions.get(device_id)
        if not pending or switch_state is None:
            return
        if switch_state.lower() == pending.get("state"):
            self._pending_actions.pop(device_id, None)

    # ---------- service helpers ----------
    async def async_suspend(self, device_id: str, suspend: bool) -> None:
        if device_id in self.states:
            self.states[device_id].suspended = suspend
            await self.async_evaluate_all()

    async def async_set_enable_control(self, enabled: bool) -> None:
        self.enable_control = enabled
        data = dict(self.entry.data)
        options = dict(self.entry.options)
        if CONF_ENABLE_CONTROL in options:
            options[CONF_ENABLE_CONTROL] = enabled
        else:
            data[CONF_ENABLE_CONTROL] = enabled
        self.hass.config_entries.async_update_entry(
            self.entry,
            data=data,
            options=options,
        )
        await self.async_evaluate_all()

    async def async_mark_manual_on(self, device_id: str) -> None:
        if device_id not in self.states:
            return
        cfg = self.configs[device_id]
        self.states[device_id].manual_on_until_ts = (
            dt_util.utcnow().timestamp() + cfg.manual_on_cooldown_seconds
        )
        await self.async_evaluate_all()

    async def async_apply_now(self, device_id: str | None = None) -> None:
        prev = self.enable_control
        self.enable_control = True
        try:
            if device_id:
                cfg = self.configs.get(device_id)
                if cfg is None:
                    return
                ctx = self._build_context()
                self.last_update_ts = ctx.now_ts
                self.last_context = {
                    "presence": ctx.presence,
                    "bio": ctx.bio,
                    "day_phase": ctx.day_phase,
                    "media_context": ctx.media_context,
                    "entertainment_active": ctx.entertainment_active,
                    "activity": ctx.activity,
                }
                await self._async_evaluate_one(cfg, ctx)
                await self._async_save()
                for cb in self._listeners:
                    cb()
                return
            await self.async_evaluate_all()
        finally:
            self.enable_control = prev

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _kind_widget(self, cfg: DeviceConfig, st: DeviceState) -> dict[str, Any]:
        if cfg.kind in ("tablet", "blind"):
            batt = _safe_float(st.battery_pct)
            dec = self.decisions.get(cfg.device_id)
            return {
                "type": "tablet",
                "battery_pct": batt,
                "low": cfg.tablet_low,
                "high": cfg.tablet_high,
                "guard": batt is not None and batt < 20,
                "display_entity": cfg.display_entity,
                "display_state": st.display_state,
                "desired_display_state": dec.desired_display_state if dec else DESIRED_KEEP,
            }
        if cfg.kind == "diffuser":
            duration = (
                cfg.diffuser_on_minutes * 60
                if st.diffuser_phase == "on"
                else cfg.diffuser_off_minutes * 60
            )
            elapsed = 0.0
            if self.last_update_ts is not None:
                elapsed = max(0.0, self.last_update_ts - (st.diffuser_phase_since_ts or self.last_update_ts))
            return {
                "type": "diffuser",
                "phase": st.diffuser_phase,
                "countdown_s": max(0, int(duration - elapsed)),
            }
        if cfg.kind == "pc":
            remaining = 0
            if st.manual_on_until_ts and self.last_update_ts is not None:
                remaining = max(0, int(st.manual_on_until_ts - self.last_update_ts))
            return {"type": "pc", "cooldown_remaining_s": remaining}
        return {"type": cfg.kind}

    def device_status(self, device_id: str) -> dict[str, Any]:
        cfg = self.configs[device_id]
        st = self.states[device_id]
        dec = self.decisions.get(device_id)
        return {
            "device_id": device_id,
            "name": cfg.name,
            "kind": cfg.kind,
            "policy": cfg.policy,
            "switch_entity": cfg.switch_entity,
            "switch_state": st.switch_state,
            "power_w": dec.power_w if dec else _safe_float(st.power_w),
            "metered": cfg.power_entity is not None
            and self.hass.states.get(cfg.power_entity) is not None,
            "active_state": dec.active_state if dec else "unknown",
            "battery_pct": _safe_float(st.battery_pct),
            "desired_switch_state": dec.desired_switch_state if dec else DESIRED_KEEP,
            "desired_display_state": dec.desired_display_state if dec else DESIRED_KEEP,
            "display_entity": cfg.display_entity,
            "display_state": st.display_state,
            "reason": dec.reason if dec else "not evaluated yet",
            "blockers": list(dec.blockers) if dec else [],
            "thresholds": {
                "active": cfg.active_threshold,
                "idle": cfg.idle_threshold,
                "deadband_lower": cfg.deadband_lower,
                "deadband_upper": cfg.deadband_upper,
            },
            "stable_off_remaining_s": dec.stable_off_remaining_s if dec else None,
            "allowed_contexts": list(cfg.allowed_contexts),
            "suspended": st.suspended,
            "context_snapshot": dict(dec.context) if dec else dict(self.last_context),
            "kind_widget": self._kind_widget(cfg, st),
            "last_action": self.last_action.get(device_id, {}),
        }

    def status_snapshot(self) -> dict[str, Any]:
        devices = [self.device_status(dev_id) for dev_id in self.configs]
        global_status = {
            "enable_control": self.enable_control,
            "context": dict(self.last_context),
            "last_update_ts": self.last_update_ts,
        }
        return {
            "global": global_status,
            "devices": devices,
            "debug_export": {
                "global": global_status,
                "devices": devices,
                "last_action": dict(self.last_action),
                "entry_id": self.entry.entry_id,
            },
        }

    async def _async_save(self) -> None:
        await self._store.async_save({
            "devices": {
                dev_id: {
                    "manual_on_until_ts": st.manual_on_until_ts,
                    "last_idle_since_ts": st.last_idle_since_ts,
                    "diffuser_phase": st.diffuser_phase,
                    "diffuser_phase_since_ts": st.diffuser_phase_since_ts,
                    "suspended": st.suspended,
                    "last_action": self.last_action.get(dev_id, {}),
                }
                for dev_id, st in self.states.items()
            }
        })


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- lookups


def coordinator_from_hass(hass: HomeAssistant, entry_id: str) -> PlugPolicyCoordinator | None:
    bucket = hass.data.get(DOMAIN, {}).get(DATA_ENTRIES, {}).get(entry_id)
    if not bucket:
        return None
    return bucket.get("coordinator")


def all_plug_policy_coordinators(hass: HomeAssistant) -> list[PlugPolicyCoordinator]:
    out: list[PlugPolicyCoordinator] = []
    for bucket in hass.data.get(DOMAIN, {}).get(DATA_ENTRIES, {}).values():
        if bucket.get("module_id") != MODULE_ID:
            continue
        c = bucket.get("coordinator")
        if c is not None:
            out.append(c)
    return out


def coordinator_for_device(hass: HomeAssistant, device_id: str) -> PlugPolicyCoordinator | None:
    for c in all_plug_policy_coordinators(hass):
        if device_id in c.configs:
            return c
    return None
