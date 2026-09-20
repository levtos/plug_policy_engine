"""Unit tests for the Plug Policy decision engine — HA-free."""

from __future__ import annotations

import pytest

import pp_const as C
import pp_engine as E


# --------------------------------------------------------------- builders


def _ctx(**kw):
    return E.GlobalContext(**kw)


def _cfg(**overrides):
    base = dict(
        device_id="dev1",
        name="Test Device",
        switch_entity="switch.test",
        policy=C.POLICY_HB,
        kind=C.KIND_GENERIC,
    )
    base.update(overrides)
    return E.DeviceConfig(**base)


def _state(**overrides):
    s = E.DeviceState()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# --------------------------------------------------------------- AO / CS


def test_ao_keeps_on_when_already_on():
    d = E.evaluate(_cfg(policy=C.POLICY_AO), _state(switch_state="on"), _ctx())
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "already on" in d.reason


def test_ao_turns_on_when_off():
    d = E.evaluate(_cfg(policy=C.POLICY_AO), _state(switch_state="off"), _ctx())
    assert d.desired_switch_state == C.DESIRED_ON


def test_cs_behaves_like_ao_for_charging_safe():
    d = E.evaluate(_cfg(policy=C.POLICY_CS), _state(switch_state="off"), _ctx())
    assert d.desired_switch_state == C.DESIRED_ON
    assert "CS" in d.reason


# --------------------------------------------------------------- HB / AC


def test_hb_protects_active_device_even_when_away():
    cfg = _cfg(policy=C.POLICY_HB, power_entity="sensor.p", active_threshold=5.0)
    st = _state(switch_state="on", power_w=80.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "active" in d.blockers


def test_hb_keeps_idle_truly_away_because_hb_is_baseline_not_away_cut():
    """HB = Home Baseline must NEVER auto-off, not even on truly-away
    + idle. Only AC (Away Cut) is allowed to schedule that cut."""
    cfg = _cfg(policy=C.POLICY_HB, power_entity="sensor.p", idle_threshold=2.0)
    st = _state(switch_state="on", power_w=0.5)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "HB" in d.reason and "away" in d.reason.lower()


def test_hb_does_not_cut_when_at_parents():
    """bei_eltern is home-equivalent — never trigger an away cut."""
    cfg = _cfg(policy=C.POLICY_HB, power_entity="sensor.p", idle_threshold=2.0)
    st = _state(switch_state="on", power_w=0.5)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AT_PARENTS))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "bei_eltern" in d.blockers[0]


def test_ac_cuts_when_truly_away_idle():
    cfg = _cfg(
        policy=C.POLICY_AC,
        power_entity="sensor.p",
        idle_threshold=2.0,
        stable_off_seconds=0,
    )
    st = _state(switch_state="on", power_w=0.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_OFF


def test_ac_waits_for_stable_off_before_cut():
    cfg = _cfg(
        policy=C.POLICY_AC,
        power_entity="sensor.p",
        idle_threshold=2.0,
        stable_off_seconds=300,
    )
    st = _state(switch_state="on", power_w=0.0, last_idle_since_ts=1000.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY, now_ts=1120.0))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert d.stable_off_remaining_s == 180
    assert "stable_off" in d.blockers


def test_ac_cuts_after_stable_off_elapsed():
    cfg = _cfg(
        policy=C.POLICY_AC,
        power_entity="sensor.p",
        idle_threshold=2.0,
        stable_off_seconds=300,
    )
    st = _state(switch_state="on", power_w=0.0, last_idle_since_ts=1000.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY, now_ts=1300.0))
    assert d.desired_switch_state == C.DESIRED_OFF
    assert d.stable_off_remaining_s == 0


# --------------------------------------------------------------- SC


def test_sc_turns_on_when_home_awake_and_allowed_phase():
    cfg = _cfg(policy=C.POLICY_SC, allowed_contexts=[C.DAY_DAY, C.DAY_EVENING])
    d = E.evaluate(
        cfg, _state(switch_state="off"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE, day_phase=C.DAY_DAY),
    )
    assert d.desired_switch_state == C.DESIRED_ON


def test_sc_cuts_when_phase_not_allowed():
    cfg = _cfg(policy=C.POLICY_SC, allowed_contexts=[C.DAY_EVENING])
    d = E.evaluate(
        cfg, _state(switch_state="on"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_AWAKE, day_phase=C.DAY_MORNING),
    )
    assert d.desired_switch_state == C.DESIRED_OFF
    assert "day_phase" in d.reason


def test_sc_cuts_when_user_is_sleeping():
    cfg = _cfg(policy=C.POLICY_SC, allowed_contexts=[C.DAY_DAY])
    d = E.evaluate(
        cfg, _state(switch_state="on"),
        _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_SLEEP, day_phase=C.DAY_DAY),
    )
    assert d.desired_switch_state == C.DESIRED_OFF


# --------------------------------------------------------------- PC kind


def test_pc_protected_when_active():
    cfg = _cfg(kind=C.KIND_PC, policy=C.POLICY_HB, power_entity="sensor.p")
    st = _state(switch_state="on", power_w=120)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY, bio=C.BIO_SLEEP))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "pc_active" in d.blockers


def test_pc_cut_when_sleeping_and_idle():
    cfg = _cfg(kind=C.KIND_PC, policy=C.POLICY_HB,
               power_entity="sensor.p", idle_threshold=2.0,
               stable_off_seconds=0)
    st = _state(switch_state="on", power_w=0.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_SLEEP))
    assert d.desired_switch_state == C.DESIRED_OFF


def test_pc_manual_cooldown_blocks_cut():
    cfg = _cfg(kind=C.KIND_PC, policy=C.POLICY_HB,
               power_entity="sensor.p", idle_threshold=2.0,
               manual_on_cooldown_seconds=900)
    st = _state(switch_state="on", power_w=0.0, manual_on_until_ts=2000.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_HOME, bio=C.BIO_SLEEP, now_ts=1500.0))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "manual_on_cooldown" in d.blockers


# --------------------------------------------------------------- appliance


def test_appliance_never_interrupted_while_active():
    cfg = _cfg(kind=C.KIND_APPLIANCE, policy=C.POLICY_HB,
               power_entity="sensor.p", active_threshold=5.0)
    st = _state(switch_state="on", power_w=1200)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "program_running" in d.blockers


def test_appliance_unknown_power_protected():
    cfg = _cfg(kind=C.KIND_APPLIANCE, policy=C.POLICY_HB,
               power_entity="sensor.p",
               unknown_behavior=C.UNK_ASSUME_ACTIVE)
    st = _state(switch_state="on", power_w="unknown")
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP


def test_appliance_idle_and_away_keeps_under_hb():
    """HB on an appliance never schedules an away-cut either.
    Use AC for actual cuts (see next test)."""
    cfg = _cfg(kind=C.KIND_APPLIANCE, policy=C.POLICY_HB,
               power_entity="sensor.p", idle_threshold=2.0,
               unknown_behavior=C.UNK_ASSUME_IDLE)
    st = _state(switch_state="on", power_w=0.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP


def test_appliance_idle_and_away_cuts_under_ac():
    cfg = _cfg(kind=C.KIND_APPLIANCE, policy=C.POLICY_AC,
               power_entity="sensor.p", idle_threshold=2.0,
               unknown_behavior=C.UNK_ASSUME_IDLE,
               stable_off_seconds=0)
    st = _state(switch_state="on", power_w=0.0)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_OFF


# --------------------------------------------------------------- bias light


def test_bias_light_does_not_turn_on_for_pc_gaming_when_tv_is_off():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="off"),
                   _ctx(
                       media_context="gaming",
                       gaming_source="pc",
                       entertainment_active=True,
                       tv_active=False,
                   ))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "tv=off" in d.blockers


def test_bias_light_on_when_tv_master_is_on_even_if_media_is_idle():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="off"),
                   _ctx(
                       media_context="idle",
                       gaming_source="none",
                       entertainment_active=False,
                       tv_active=True,
                   ))
    assert d.desired_switch_state == C.DESIRED_ON


def test_bias_light_stays_on_across_media_context_transitions_while_tv_is_on():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    contexts = (
        _ctx(media_context="idle", gaming_source="none", tv_active=True),
        _ctx(media_context="gaming", gaming_source="tv", tv_active=True),
        _ctx(media_context="movie", gaming_source="none", tv_active=True),
        _ctx(media_context="music", gaming_source="pc", tv_active=True),
    )
    for ctx in contexts:
        d = E.evaluate(cfg, _state(switch_state="on"), ctx)
        assert d.desired_switch_state == C.DESIRED_KEEP


def test_bias_light_off_when_tv_master_is_off_even_if_media_is_tv():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="on"),
                   _ctx(media_context="movie", entertainment_active=True,
                        tv_active=False))
    assert d.desired_switch_state == C.DESIRED_OFF
    assert "tv=off" in d.blockers


def test_bias_light_sleep_blocks_entertainment_active():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="on"),
                   _ctx(bio=C.BIO_SLEEP, media_context="movie", entertainment_active=True,
                        tv_active=True))
    assert d.desired_switch_state == C.DESIRED_OFF
    assert "bio=sleep" in d.blockers


def test_bias_light_awake_after_sleep_follows_tv_master_again():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="off"),
                   _ctx(bio=C.BIO_AWAKE, media_context="idle",
                        entertainment_active=False, tv_active=True))
    assert d.desired_switch_state == C.DESIRED_ON


def test_bias_light_tv_unknown_holds_on_state():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="on"),
                   _ctx(media_context="gaming", gaming_source="tv",
                        entertainment_active=True, tv_active=None))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "tv=unknown" in d.blockers


def test_bias_light_tv_unknown_holds_off_state():
    cfg = _cfg(kind=C.KIND_BIAS_LIGHT, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="off"),
                   _ctx(media_context="movie", tv_active=None))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "tv=unknown" in d.blockers


# --------------------------------------------------------------- diffuser


def test_diffuser_stops_on_sleep():
    cfg = _cfg(kind=C.KIND_DIFFUSER, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="on"),
                   _ctx(bio=C.BIO_SLEEP, presence=C.PRESENCE_HOME))
    assert d.desired_switch_state == C.DESIRED_OFF
    assert "bio=sleep" in d.blockers


def test_diffuser_stops_at_night_phase():
    cfg = _cfg(kind=C.KIND_DIFFUSER, policy=C.POLICY_HB)
    d = E.evaluate(cfg, _state(switch_state="on"),
                   _ctx(bio=C.BIO_AWAKE, presence=C.PRESENCE_HOME, day_phase=C.DAY_NIGHT))
    assert d.desired_switch_state == C.DESIRED_OFF


def test_diffuser_cycles_on_then_off():
    cfg = _cfg(kind=C.KIND_DIFFUSER, policy=C.POLICY_HB,
               diffuser_on_minutes=15, diffuser_off_minutes=15)
    on_phase = _state(switch_state="on", diffuser_phase="on", diffuser_phase_since_ts=0.0)
    # 16 minutes into on-phase → switch to off cycle
    d = E.evaluate(cfg, on_phase,
                   _ctx(bio=C.BIO_AWAKE, presence=C.PRESENCE_HOME,
                        day_phase=C.DAY_DAY, now_ts=16 * 60))
    assert d.desired_switch_state == C.DESIRED_OFF
    assert "on-phase elapsed" in d.reason


# --------------------------------------------------------------- tablet


def test_tablet_deep_discharge_guard_charges_under_20pct():
    cfg = _cfg(kind=C.KIND_TABLET, policy=C.POLICY_HB,
               tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(cfg, _state(switch_state="off", battery_pct=15),
                   _ctx())
    assert d.desired_switch_state == C.DESIRED_ON
    assert "deep_discharge_guard" in d.blockers


def test_tablet_charges_below_low_threshold():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(cfg, _state(switch_state="off", battery_pct=35), _ctx())
    assert d.desired_switch_state == C.DESIRED_ON


def test_tablet_stops_charging_at_high_threshold():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(cfg, _state(switch_state="on", battery_pct=82), _ctx())
    assert d.desired_switch_state == C.DESIRED_OFF


def test_tablet_holds_in_hysteresis_zone():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(cfg, _state(switch_state="on", battery_pct=60), _ctx())
    assert d.desired_switch_state == C.DESIRED_KEEP


def test_tablet_battery_unavailable_overrides_suspend_for_charging():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(
        cfg,
        _state(switch_state="off", battery_pct="unavailable", suspended=True),
        _ctx(),
    )
    assert d.desired_switch_state == C.DESIRED_ON
    assert "policy_suspended_bypassed" in d.blockers


def test_tablet_battery_unknown_overrides_suspend_for_charging():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(
        cfg,
        _state(switch_state="off", battery_pct="unknown", suspended=True),
        _ctx(),
    )
    assert d.desired_switch_state == C.DESIRED_ON
    assert "policy_suspended_bypassed" in d.blockers


def test_tablet_not_needing_charge_keeps_suspend_behavior():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(
        cfg,
        _state(switch_state="on", battery_pct=60, suspended=True),
        _ctx(),
    )
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "policy_suspended" in d.blockers


def test_tablet_deep_discharge_overrides_suspend():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(
        cfg,
        _state(switch_state="off", battery_pct=12, suspended=True),
        _ctx(),
    )
    assert d.desired_switch_state == C.DESIRED_ON
    assert "deep_discharge_guard" in d.blockers
    assert "policy_suspended_bypassed" in d.blockers


def test_tablet_low_battery_overrides_suspend_for_charging():
    cfg = _cfg(kind=C.KIND_TABLET, tablet_low=40, tablet_high=80,
               battery_entity="sensor.batt")
    d = E.evaluate(
        cfg,
        _state(switch_state="off", battery_pct=35, suspended=True),
        _ctx(),
    )
    assert d.desired_switch_state == C.DESIRED_ON
    assert "policy_suspended_bypassed" in d.blockers


# --------------------------------------------------------------- blind charger


def test_blind_charges_below_low_threshold():
    cfg = _cfg(kind=C.KIND_BLIND, tablet_low=30, tablet_high=80,
               battery_entity="sensor.cover_batt")
    d = E.evaluate(cfg, _state(switch_state="off", battery_pct=25), _ctx())
    assert d.desired_switch_state == C.DESIRED_ON
    assert d.reason.startswith("blind:")


def test_blind_stops_charging_at_high_threshold():
    cfg = _cfg(kind=C.KIND_BLIND, tablet_low=30, tablet_high=80,
               battery_entity="sensor.cover_batt")
    d = E.evaluate(cfg, _state(switch_state="on", battery_pct=82), _ctx())
    assert d.desired_switch_state == C.DESIRED_OFF


def test_blind_holds_in_hysteresis_zone():
    cfg = _cfg(kind=C.KIND_BLIND, tablet_low=30, tablet_high=80,
               battery_entity="sensor.cover_batt")
    d = E.evaluate(cfg, _state(switch_state="on", battery_pct=60), _ctx())
    assert d.desired_switch_state == C.DESIRED_KEEP


def test_blind_battery_unavailable_does_not_stick_off():
    # Fully discharged cover → battery sensor unavailable. Must recover, not hang off.
    cfg = _cfg(kind=C.KIND_BLIND, tablet_low=30, tablet_high=80,
               battery_entity="sensor.cover_batt")
    d_off = E.evaluate(cfg, _state(switch_state="off", battery_pct="unavailable"), _ctx())
    assert d_off.desired_switch_state == C.DESIRED_ON
    d_on = E.evaluate(cfg, _state(switch_state="on", battery_pct="unavailable"), _ctx())
    assert d_on.desired_switch_state == C.DESIRED_KEEP


def test_blind_deep_discharge_guard_under_20pct():
    cfg = _cfg(kind=C.KIND_BLIND, tablet_low=30, tablet_high=80,
               battery_entity="sensor.cover_batt")
    d = E.evaluate(cfg, _state(switch_state="off", battery_pct=12), _ctx())
    assert d.desired_switch_state == C.DESIRED_ON
    assert "deep_discharge_guard" in d.blockers


# --------------------------------------------------------------- suspended


def test_suspended_device_is_never_acted_on():
    cfg = _cfg(policy=C.POLICY_HB, power_entity="sensor.p", idle_threshold=2.0)
    st = _state(switch_state="on", power_w=0.0, suspended=True)
    d = E.evaluate(cfg, st, _ctx(presence=C.PRESENCE_AWAY))
    assert d.desired_switch_state == C.DESIRED_KEEP
    assert "policy_suspended" in d.blockers


# --------------------------------------------------------------- coffee


def test_coffee_maker_is_wake_signal_only():
    cfg = _cfg(kind=C.KIND_COFFEE, policy=C.POLICY_AO)
    d = E.evaluate(cfg, _state(switch_state="off"), _ctx(),
                   ha_just_started=True)
    assert d.desired_switch_state == C.DESIRED_ON
    assert "wake indicator" in d.reason


# --------------------------------------------------------------- registry cleanup


def test_device_dev_id_from_identifier_parses_and_protects_hub():
    mod, entry = "plug_policy_engine", "01ENTRY"
    # Per-device identifier → dev_id extracted (handles underscores in dev_id).
    assert C.device_dev_id_from_identifier(
        f"{mod}_{entry}_dev_603c3dbe", mod, entry) == "dev_603c3dbe"
    assert C.device_dev_id_from_identifier(
        f"{mod}_{entry}_living_switch_plug", mod, entry) == "living_switch_plug"
    # Hub identifier → None (must never be treated as a removable device).
    assert C.device_dev_id_from_identifier(f"{mod}_{entry}", mod, entry) is None
    # Foreign entry → None.
    assert C.device_dev_id_from_identifier(
        f"{mod}_OTHER_dev_x", mod, entry) is None
