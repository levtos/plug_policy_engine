"""Pure decision engine for plug_policy_engine.

This module has **no** Home Assistant imports. It receives plain dataclasses
and returns a Decision. All branching for AO/HB/AC/SC/CS/SPECIAL and the
special device kinds (PC, appliance, diffuser, tablet, bias light, ...)
lives here so it can be unit-tested without HA.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, replace as dc_replace
import math
from typing import Any, Optional

from .const import (
    POLICY_AO, POLICY_HB, POLICY_AC, POLICY_SC, POLICY_CS, POLICY_SPECIAL,
    KIND_PC, KIND_APPLIANCE, KIND_COFFEE, KIND_BIAS_LIGHT, KIND_DIFFUSER,
    KIND_TABLET, KIND_BLIND, KIND_DENON, KIND_H14_DOCK, KIND_GENERIC,
    PRESENCE_HOME, PRESENCE_AWAY, PRESENCE_AT_PARENTS,
    BIO_AWAKE, BIO_SLEEP,
    DAY_NIGHT, DAY_PHASE_ALIASES,
    UNK_ASSUME_ACTIVE, UNK_ASSUME_IDLE,
    DESIRED_ON, DESIRED_OFF, DESIRED_KEEP,
    DEFAULT_ACTIVE_THRESHOLD, DEFAULT_IDLE_THRESHOLD,
    DEFAULT_TABLET_LOW, DEFAULT_TABLET_HIGH,
    DEFAULT_DIFFUSER_ON, DEFAULT_DIFFUSER_OFF,
    DEFAULT_STABLE_OFF, DEFAULT_MANUAL_COOLDOWN,
)


def _normalise_day_phase(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip().lower()
    return DAY_PHASE_ALIASES.get(key, value)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GlobalContext:
    presence: Optional[str] = None       # zuhause / abwesend / bei_eltern
    bio: Optional[str] = None            # awake / sleep
    day_phase: Optional[str] = None      # morning/day/evening/night
    media_context: Optional[str] = None  # e.g. "movie", "music", "idle"
    gaming_source: Optional[str] = None  # e.g. "tv", "pc", "none"
    entertainment_active: Optional[bool] = None
    activity: Optional[str] = None
    # Authoritative TV truth (core_devices master). False = verifiably off;
    # None = unbound/unknown/unavailable, so the bias-light state is held.
    tv_active: Optional[bool] = None
    now_ts: float = 0.0                  # seconds since epoch (testable)

    @property
    def is_truly_away(self) -> bool:
        """Only abwesend triggers Away-Cut. bei_eltern is treated as 'at home'."""
        return self.presence == PRESENCE_AWAY

    @property
    def is_home_like(self) -> bool:
        return self.presence in (PRESENCE_HOME, PRESENCE_AT_PARENTS)

    @property
    def asleep(self) -> bool:
        return self.bio == BIO_SLEEP


@dataclass
class DeviceConfig:
    device_id: str
    name: str
    switch_entity: str
    policy: str = POLICY_HB
    kind: str = KIND_GENERIC
    power_entity: Optional[str] = None
    battery_entity: Optional[str] = None
    display_entity: Optional[str] = None   # Tablet: Screen-Aktor (None = Display-Steuerung aus)
    active_threshold: float = DEFAULT_ACTIVE_THRESHOLD
    idle_threshold: float = DEFAULT_IDLE_THRESHOLD
    deadband_lower: Optional[float] = None
    deadband_upper: Optional[float] = None
    stable_off_seconds: int = DEFAULT_STABLE_OFF
    unknown_behavior: str = UNK_ASSUME_ACTIVE
    allowed_contexts: list = field(default_factory=list)
    never_cut_when_active: bool = True
    wake_signal_only: bool = False
    tablet_low: int = DEFAULT_TABLET_LOW
    tablet_high: int = DEFAULT_TABLET_HIGH
    diffuser_on_minutes: int = DEFAULT_DIFFUSER_ON
    diffuser_off_minutes: int = DEFAULT_DIFFUSER_OFF
    manual_on_cooldown_seconds: int = DEFAULT_MANUAL_COOLDOWN


@dataclass
class DeviceState:
    switch_state: Optional[str] = None         # "on"/"off"/None
    power_w: Any = None                        # float | "unknown"/"unavailable"/None
    active_hint: Any = None                    # active / idle hint from semantic source attrs
    battery_pct: Any = None
    display_state: Optional[str] = None        # "on"/"off"/None — aktueller Screen-Zustand
    last_idle_since_ts: Optional[float] = None # when did we first see idle continuously
    manual_on_until_ts: Optional[float] = None # PC cooldown
    diffuser_phase: str = "off"                # "on" | "off"
    diffuser_phase_since_ts: float = 0.0
    suspended: bool = False                    # service: suspend_device_policy


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    device_id: str
    policy: str
    kind: str
    desired_switch_state: str    # on / off / keep
    desired_display_state: str = DESIRED_KEEP  # Tablet-Screen: on / off / keep (sonst keep)
    active_state: str = "unknown"  # active / idle / unknown
    reason: str = ""
    blockers: list = field(default_factory=list)
    power_w: Any = None
    context: dict = field(default_factory=dict)
    stable_off_remaining_s: Optional[int] = None

    def to_attrs(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_UNKNOWN_STRINGS = {"unknown", "unavailable", "none", ""}
_ACTIVE_STRINGS = {"active", "on", "playing", "true"}
_IDLE_STRINGS = {"idle", "off", "standby", "false", "inactive"}


def _power_float(p: Any) -> Optional[float]:
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    s = str(p).strip().lower()
    if s in _UNKNOWN_STRINGS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _battery_int(b: Any) -> Optional[int]:
    if b is None:
        return None
    if isinstance(b, (int, float)):
        return int(b)
    s = str(b).strip().lower()
    if s in _UNKNOWN_STRINGS:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _tablet_charge_overrides_suspend(cfg: DeviceConfig, state: DeviceState) -> bool:
    if cfg.kind != KIND_TABLET:
        return False
    batt = _battery_int(state.battery_pct)
    return batt is None or batt < cfg.tablet_low


def _classify_active(cfg: DeviceConfig, state: DeviceState) -> str:
    """Return 'active' / 'idle' / 'unknown'."""
    if isinstance(state.active_hint, bool):
        return "active" if state.active_hint else "idle"
    if isinstance(state.active_hint, str):
        semantic = state.active_hint.strip().lower()
        if semantic in _ACTIVE_STRINGS:
            return "active"
        if semantic in _IDLE_STRINGS:
            return "idle"
    if isinstance(state.power_w, str):
        semantic = state.power_w.strip().lower()
        if semantic in _ACTIVE_STRINGS:
            return "active"
        if semantic in _IDLE_STRINGS:
            return "idle"
    p = _power_float(state.power_w)
    if p is None:
        # No power sensor at all → unknown
        if cfg.power_entity is None and state.power_w is None:
            return "unknown"
        if cfg.unknown_behavior == UNK_ASSUME_IDLE:
            return "idle"
        return "unknown"  # safer default; treated as active where it matters
    # With deadband: only flip to idle below deadband_lower, only to active above deadband_upper
    if cfg.deadband_lower is not None and cfg.deadband_upper is not None:
        if p >= cfg.deadband_upper:
            return "active"
        if p <= cfg.deadband_lower:
            return "idle"
        return "idle"  # in-band → treat as idle, but engine should hold via stable_off
    if p >= cfg.active_threshold:
        return "active"
    if p <= cfg.idle_threshold:
        return "idle"
    return "idle"


def _is_protected_active(active_state: str, cfg: DeviceConfig) -> bool:
    """For unknown + risky-to-cut devices we treat as active."""
    if active_state == "active":
        return True
    if active_state == "unknown" and cfg.unknown_behavior == UNK_ASSUME_ACTIVE:
        return True
    return False


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def evaluate(
    cfg: DeviceConfig,
    state: DeviceState,
    ctx: GlobalContext,
    *,
    ha_just_started: bool = False,
) -> Decision:
    """Top-level dispatch."""
    blockers: list[str] = []
    # Fold detailed day-phase variants (early_morning, late_evening, …)
    # into the coarse vocabulary the rules below compare against.
    normalised_phase = _normalise_day_phase(ctx.day_phase)
    if normalised_phase != ctx.day_phase:
        ctx = dc_replace(ctx, day_phase=normalised_phase)
    active_state = _classify_active(cfg, state)
    power = _power_float(state.power_w)

    context_snapshot = {
        "presence": ctx.presence,
        "bio": ctx.bio,
        "day_phase": ctx.day_phase,
        "media_context": ctx.media_context,
        "gaming_source": ctx.gaming_source,
        "entertainment_active": ctx.entertainment_active,
        "activity": ctx.activity,
        "tv_active": ctx.tv_active,
    }

    def make(desired: str, reason: str, extra_blockers: list[str] | None = None) -> Decision:
        return Decision(
            device_id=cfg.device_id,
            policy=cfg.policy,
            kind=cfg.kind,
            desired_switch_state=desired,
            active_state=active_state,
            reason=reason,
            blockers=list(blockers) + (extra_blockers or []),
            power_w=power,
            context=context_snapshot,
        )

    def stable_off_gate(decision: Decision) -> Decision:
        """Delay idle-driven cuts until the device stayed idle long enough."""
        if decision.desired_switch_state != DESIRED_OFF:
            return decision
        if active_state != "idle" or cfg.stable_off_seconds <= 0:
            return decision

        idle_since = state.last_idle_since_ts
        elapsed = 0.0 if idle_since is None else max(0.0, ctx.now_ts - idle_since)
        remaining = max(0, int(math.ceil(cfg.stable_off_seconds - elapsed)))
        if remaining <= 0:
            return dc_replace(decision, stable_off_remaining_s=0)

        blockers_waiting = list(decision.blockers)
        if "stable_off" not in blockers_waiting:
            blockers_waiting.append("stable_off")
        return dc_replace(
            decision,
            desired_switch_state=DESIRED_KEEP,
            reason=f"stable-off waiting {remaining}s before cut: {decision.reason}",
            blockers=blockers_waiting,
            stable_off_remaining_s=remaining,
        )

    # Service: policy suspended → never act
    if state.suspended and not _tablet_charge_overrides_suspend(cfg, state):
        blockers.append("policy_suspended")
        return make(DESIRED_KEEP, "policy suspended for this device")
    if state.suspended:
        blockers.append("policy_suspended_bypassed")

    # Wake-signal-only devices (coffee maker) report decisions but never schedule cuts.
    if cfg.wake_signal_only or cfg.kind == KIND_COFFEE:
        # Coffee maker is AO → just ensure on at startup
        if cfg.policy == POLICY_AO and ha_just_started and state.switch_state != "on":
            return make(DESIRED_ON, "AO startup ensure-on (wake indicator)")
        return make(DESIRED_KEEP, "wake-signal-only device, no schedule action")

    # Device-kind specialisations (these may short-circuit policy)
    if cfg.kind == KIND_TABLET:
        return stable_off_gate(_decide_tablet(cfg, state, ctx, make))
    if cfg.kind == KIND_BLIND:
        # Blind charger: battery-keeper only, no display/screen control.
        # Reuses the tablet charge logic (tablet_low/high + <20% guard +
        # unavailable→safe-on) against the cover's battery sensor.
        return stable_off_gate(_decide_tablet_charge(cfg, state, ctx, make))
    if cfg.kind == KIND_DIFFUSER:
        return stable_off_gate(_decide_diffuser(cfg, state, ctx, make))
    if cfg.kind == KIND_BIAS_LIGHT:
        return stable_off_gate(_decide_bias_light(cfg, state, ctx, make))
    if cfg.kind == KIND_PC:
        return stable_off_gate(_decide_pc(cfg, state, ctx, active_state, make))
    if cfg.kind == KIND_APPLIANCE:
        return stable_off_gate(_decide_appliance(cfg, state, ctx, active_state, make))

    # Generic policy dispatch
    if cfg.policy == POLICY_AO:
        return stable_off_gate(_decide_ao(cfg, state, ha_just_started, make))
    if cfg.policy == POLICY_CS:
        # Charging-Safe ~ AO; never auto-off
        return stable_off_gate(_decide_ao(cfg, state, ha_just_started, make, label="CS"))
    if cfg.policy == POLICY_HB:
        return stable_off_gate(_decide_baseline_or_away(cfg, state, ctx, active_state, make, mode="HB"))
    if cfg.policy == POLICY_AC:
        return stable_off_gate(_decide_baseline_or_away(cfg, state, ctx, active_state, make, mode="AC"))
    if cfg.policy == POLICY_SC:
        return stable_off_gate(_decide_schedule_context(cfg, state, ctx, active_state, make))
    if cfg.policy == POLICY_SPECIAL:
        return make(DESIRED_KEEP, "SPECIAL policy: no built-in rule")

    return make(DESIRED_KEEP, f"unknown policy {cfg.policy}")


# --------------------------------------------------------------------------- #
# Per-policy / per-kind branches
# --------------------------------------------------------------------------- #
def _decide_ao(cfg, state, ha_just_started, make, *, label: str = "AO") -> Decision:
    if state.switch_state == "on":
        return make(DESIRED_KEEP, f"{label}: already on")
    if state.switch_state == "off":
        return make(DESIRED_ON, f"{label}: must always be on")
    if ha_just_started:
        return make(DESIRED_ON, f"{label}: ensure on after HA start")
    return make(DESIRED_ON, f"{label}: ensure on (state {state.switch_state!r})")


def _decide_baseline_or_away(cfg, state, ctx, active_state, make, *, mode: str) -> Decision:
    """Home Baseline vs. Away Cut.

    HB is a *baseline* policy and must never schedule an automatic
    off — even on truly-away + idle. AC is the actual cut policy:
    only AC turns the device off when the household is truly away
    and the device is idle. ``bei_eltern`` is treated as "still at
    home" for both. Active / unknown-as-active is always protected
    via ``never_cut_when_active``.
    """
    # Protect active / unknown-as-active for both HB and AC.
    if cfg.never_cut_when_active and _is_protected_active(active_state, cfg):
        return make(DESIRED_KEEP, f"{mode}: device active/unknown — never cut", ["active"])

    if mode == "HB":
        # HB never cuts. Surface a clear reason for each branch so the
        # decision sensor stays informative.
        if not ctx.is_truly_away:
            if ctx.presence == PRESENCE_AT_PARENTS:
                return make(DESIRED_KEEP, "HB: bei_eltern is not a real away — no cut",
                            ["presence=bei_eltern"])
            return make(DESIRED_KEEP, f"HB: presence not away ({ctx.presence!r})")
        # Truly away + idle: HB still keeps. AC handles the actual cut.
        return make(DESIRED_KEEP, "HB: away + idle — no baseline action (HB is not an away-cut policy)")

    # AC: the real away-cut policy.
    if not ctx.is_truly_away:
        if ctx.presence == PRESENCE_AT_PARENTS:
            return make(DESIRED_KEEP, "AC: bei_eltern is not a real away — no cut",
                        ["presence=bei_eltern"])
        return make(DESIRED_KEEP, f"AC: presence not away ({ctx.presence!r})")

    # Truly away + idle → cut.
    if state.switch_state == "off":
        return make(DESIRED_KEEP, "AC: already off while away")
    return make(DESIRED_OFF, "AC: away + idle → cut")


def _decide_schedule_context(cfg, state, ctx, active_state, make) -> Decision:
    """Schedule Context: only on when (allowed_contexts) match — i.e. awake + at home + allowed day phase."""
    reasons = []
    allowed = bool(cfg.allowed_contexts)
    home_ok = ctx.is_home_like
    awake_ok = ctx.bio == BIO_AWAKE
    phase_ok = (not allowed) or (ctx.day_phase in cfg.allowed_contexts)

    if not home_ok:
        reasons.append(f"presence={ctx.presence!r}")
    if not awake_ok:
        reasons.append(f"bio={ctx.bio!r}")
    if not phase_ok:
        reasons.append(f"day_phase={ctx.day_phase!r} not in {cfg.allowed_contexts}")

    if home_ok and awake_ok and phase_ok:
        if state.switch_state == "on":
            return make(DESIRED_KEEP, "SC: window open and already on")
        return make(DESIRED_ON, "SC: window open")
    if state.switch_state == "off":
        return make(DESIRED_KEEP, "SC: outside allowed context, already off")
    return make(DESIRED_OFF, "SC: outside allowed context — " + ", ".join(reasons))


# ---- Special kinds -------------------------------------------------------- #
def _decide_pc(cfg, state, ctx, active_state, make) -> Decision:
    # Rule: PC active → never auto off
    if _is_protected_active(active_state, cfg):
        return make(DESIRED_KEEP, "PC active or unknown — protected", ["pc_active"])
    # Manual-on cooldown
    if state.manual_on_until_ts and ctx.now_ts < state.manual_on_until_ts:
        remaining = int(state.manual_on_until_ts - ctx.now_ts)
        return make(DESIRED_KEEP, f"PC manual-on cooldown ({remaining}s left)",
                    ["manual_on_cooldown"])
    # Idle + sleep → off (only when truly idle and user is asleep)
    if ctx.asleep and active_state == "idle":
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "PC already off (sleep + idle)")
        return make(DESIRED_OFF, "PC idle while user asleep — safe cut")
    # Otherwise leave alone
    return make(DESIRED_KEEP, "PC: no cut condition met")


def _decide_appliance(cfg, state, ctx, active_state, make) -> Decision:
    """Washer / dryer / dishwasher: never lose a running program.

    Running or unknown power is always protected — we must never
    interrupt a programme. For idle programmes, HB is a baseline
    policy and keeps the plug as-is; only AC actually cuts on
    truly-away + idle.
    """
    # Any draw → protect
    if active_state == "active":
        return make(DESIRED_KEEP, "appliance running — never interrupt", ["program_running"])
    if active_state == "unknown":
        # If we cannot prove idle, do not cut.
        return make(DESIRED_KEEP, "appliance power unknown — protect program",
                    ["power_unknown"])
    # Idle:
    if cfg.policy == POLICY_AO:
        # Always-On: never cut, and ensure the plug is on so the appliance
        # stays (remote-)startable. Active/unknown were already protected above.
        if state.switch_state == "off":
            return make(DESIRED_ON, "AO appliance: must always be on")
        return make(DESIRED_KEEP, "AO appliance: already on — never cut")
    if cfg.policy == POLICY_AC:
        if ctx.is_truly_away:
            if state.switch_state == "off":
                return make(DESIRED_KEEP, "appliance idle + away, already off")
            return make(DESIRED_OFF, "appliance idle + truly away → cut")
        return make(DESIRED_KEEP, "appliance idle but presence not away")
    if cfg.policy == POLICY_HB:
        if ctx.is_truly_away:
            return make(DESIRED_KEEP,
                        "HB appliance: away + idle — no baseline action (HB is not an away-cut policy)")
        return make(DESIRED_KEEP, "HB appliance: idle, presence not away — keep")
    return make(DESIRED_KEEP, "appliance idle — no cut policy active")


def _decide_bias_light(cfg, state, ctx, make) -> Decision:
    """Bias Light follows the authoritative TV master, except during sleep."""
    if ctx.asleep:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "bias light: sleep — already off", ["bio=sleep"])
        return make(DESIRED_OFF, "bias light: stop on sleep", ["bio=sleep"])

    if ctx.tv_active is None:
        return make(
            DESIRED_KEEP,
            "bias light: TV master unknown — hold current state",
            ["tv=unknown"],
        )

    if ctx.tv_active is False:
        if state.switch_state == "off":
            return make(
                DESIRED_KEEP,
                "bias light: TV off — already off",
                ["tv=off"],
            )
        return make(DESIRED_OFF, "bias light: TV off", ["tv=off"])

    if state.switch_state == "on":
        return make(DESIRED_KEEP, "bias light: TV on, already on")
    return make(DESIRED_ON, "bias light: TV on")


def _decide_diffuser(cfg, state, ctx, make) -> Decision:
    """Schedule-context diffuser with 15/15 on/off cycle, only awake + home + allowed phase."""
    # Hard stop on sleep / away / night
    if ctx.asleep:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "diffuser: sleep — already off")
        return make(DESIRED_OFF, "diffuser: stop on sleep", ["bio=sleep"])
    if ctx.is_truly_away:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "diffuser: away — already off")
        return make(DESIRED_OFF, "diffuser: stop on away", ["presence=abwesend"])
    if ctx.day_phase == DAY_NIGHT:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "diffuser: night — already off")
        return make(DESIRED_OFF, "diffuser: stop at night", ["day_phase=night"])

    # Allowed contexts (day phases) filter
    if cfg.allowed_contexts and ctx.day_phase not in cfg.allowed_contexts:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, "diffuser: outside allowed phase, off")
        return make(DESIRED_OFF, f"diffuser: phase {ctx.day_phase!r} not allowed")

    # Cycle 15/15
    on_secs = cfg.diffuser_on_minutes * 60
    off_secs = cfg.diffuser_off_minutes * 60
    phase = state.diffuser_phase
    since = state.diffuser_phase_since_ts if state.diffuser_phase_since_ts is not None else ctx.now_ts
    elapsed = max(0.0, ctx.now_ts - since)
    if phase == "on":
        if elapsed >= on_secs:
            return make(DESIRED_OFF, "diffuser: on-phase elapsed → off cycle")
        if state.switch_state != "on":
            return make(DESIRED_ON, "diffuser: in on-cycle")
        return make(DESIRED_KEEP, "diffuser: on-cycle running")
    # phase == "off"
    if elapsed >= off_secs:
        return make(DESIRED_ON, "diffuser: off-phase elapsed → on cycle")
    if state.switch_state != "off":
        return make(DESIRED_OFF, "diffuser: in off-cycle")
    return make(DESIRED_KEEP, "diffuser: off-cycle running")


def _decide_tablet(cfg, state, ctx, make) -> Decision:
    """Tablet-Kind: kombiniert Lade-Policy (Plug) + Display-Policy (Screen).

    Die Plug-/Lade-Entscheidung (``desired_switch_state``) ist unverändert; die
    neue Display-Entscheidung (``desired_display_state``) hängt additiv daran und
    greift nur, wenn ein ``display_entity`` konfiguriert ist.
    """
    dec = _decide_tablet_charge(cfg, state, ctx, make)
    display = _decide_tablet_display(cfg, ctx)
    if display != DESIRED_KEEP:
        return dc_replace(dec, desired_display_state=display)
    return dec


def _decide_tablet_display(cfg, ctx) -> str:
    """Pure Screen-Entscheidung (ersetzt die alten tablet_display_*-Automationen).

    - kein display_entity → Feature aus (keep).
    - Schlaf ODER wirklich abwesend → Screen aus / Sleep-Lock (hard lock).
    - zuhause-artig UND wach → Screen an.
    - sonst (z.B. wach aber Präsenz unbekannt) → keep.
    bei_eltern zählt als zuhause-artig (kein Lock), analog zur Lade-Policy.
    """
    if not cfg.display_entity:
        return DESIRED_KEEP
    if ctx.asleep or ctx.is_truly_away:
        return DESIRED_OFF
    if ctx.is_home_like and ctx.bio == BIO_AWAKE:
        return DESIRED_ON
    return DESIRED_KEEP


def _decide_tablet_charge(cfg, state, ctx, make) -> Decision:
    """Tablet 40/80 charging. Runs 24/7, independent of presence/bio.

    Below ~20% deep-discharge protection has absolute priority.
    """
    # Shared by the tablet and blind-charger kinds; label the reason per kind.
    label = "blind" if cfg.kind == KIND_BLIND else "tablet"
    batt = _battery_int(state.battery_pct)
    if batt is None:
        # unavailable → keep charging on
        if state.switch_state == "on":
            return make(DESIRED_KEEP, f"{label}: battery unknown → keep charging on")
        return make(DESIRED_ON, f"{label}: battery unknown → safe on")

    if batt < 20:
        if state.switch_state == "on":
            return make(DESIRED_KEEP, f"{label}: deep-discharge guard (<20%) on", ["deep_discharge_guard"])
        return make(DESIRED_ON, f"{label}: deep-discharge guard (<20%)", ["deep_discharge_guard"])

    if batt < cfg.tablet_low:
        if state.switch_state == "on":
            return make(DESIRED_KEEP, f"{label}: {batt}% < {cfg.tablet_low}, charging")
        return make(DESIRED_ON, f"{label}: {batt}% < {cfg.tablet_low} → charge")
    if batt >= cfg.tablet_high:
        if state.switch_state == "off":
            return make(DESIRED_KEEP, f"{label}: {batt}% ≥ {cfg.tablet_high}, off")
        return make(DESIRED_OFF, f"{label}: {batt}% ≥ {cfg.tablet_high} → stop charge")
    # Hysteresis zone: hold whatever the switch is doing
    return make(DESIRED_KEEP, f"{label}: {batt}% inside hysteresis ({cfg.tablet_low}-{cfg.tablet_high})")
