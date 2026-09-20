# Changelog

## 0.3.5

- Issue #22: Bias-Light decisions now use `sensor.benni_master_tv` as the sole
  TV on/off truth instead of deriving a second truth from Media State and gaming
  transitions. `bio=sleep` remains the hard off override; returning to awake
  follows the TV master again.
- Unknown or unavailable TV-master input now holds the existing Bias-Light state
  and cannot create commands from stale Media-/Gaming fallbacks.

## 0.3.4

- control#35: Bias-Light re-armed itself for ~20-30 s after a real TV-off
  because the TV-stack context (`gaming`/`gaming_source=tv`) trails the TV
  master during the PS5 shutdown tail (+ feeder debounce), overriding any
  external off. The bias decision now consumes the real TV truth via a new
  optional global binding `tv_active_entity` (prefill
  `sensor.benni_master_tv`): a clean master `off` gates `want_on`
  (blocker `tv=off`), a real TV-on lifts the gate immediately
  (event-driven binding), and `unknown`/`unavailable`/unbound stays
  fail-open (unchanged behavior, no false offs on master dropouts).
- New binding is exposed in the globals options flow (strings + de/en
  translations) and in the decision/context observability attributes.

## 0.3.3

- Add the Aqara M3 Hub power output (`switch.smart_power_strip_usb_1`) to the
  Benni profile with the existing `AO` policy so an available plug observed as
  off is restored by the unchanged Always-On contract.

## 0.3.2

- FLEET-170: Tablet/dashboard charging fail-safe now bypasses policy suspend
  when battery is unknown/unavailable or below the deep-discharge guard, so
  transient suspend states cannot leave the tablet stuck off.
- Keep the non-latching auto-suspend guard for other devices and tablet
  non-charging paths, while skipping optional display service calls when the
  display target itself is unknown/unavailable.

## 0.3.1

- FLEET-220: Bias-Light policy now follows the TV stack instead of broad
  `entertainment_active`. PC gaming (`media_context=gaming`,
  `gaming_source=pc`) no longer turns on the bias-light plug, while TV,
  streaming, and TV gaming still can.
- Add `gaming_source_entity` as a global selector with Benni media-state
  prefill, options-flow label, panel visibility, and regression coverage.

## 0.2.5

- PC manual wake guard: a live `off -> on` transition of the PC plug now starts
  the existing manual-on cooldown automatically. This prevents Plug Policy from
  cutting the PC immediately during sleep before boot power crosses the active
  threshold.

## 0.2.0

- **Tablet-Display-Policy (FLEET-156).** Das `tablet`-Kind bekommt zusätzlich zur
  40/80-Lade-Policy eine Screen-Steuerung: neuer optionaler `display_entity`
  (switch/light/input_boolean). Regeln (ersetzen die alten `tablet_display_*`-
  Automationen): Schlaf ODER wirklich abwesend → Screen aus (Sleep-Lock);
  zuhause-artig UND wach → Screen an; sonst keep. `bei_eltern` zählt als zuhause.
- Additiv & rückwärtskompatibel: neue `Decision.desired_display_state`
  (Default `keep`), greift nur bei gesetztem `display_entity`. Plug-/Lade-Logik
  und alle anderen Kinds unverändert. Apply gated über `enable_control`
  (Shadow), idempotent, domain-agnostisch via `homeassistant.turn_on/off`.
- Status/Panel: `desired_display_state`, `display_state`, `display_entity` im
  `device_status` + tablet-Widget.

## 0.1.21

- Replace the rejected Plug/Power facade binding with the intended
  `sensor.benni_master_household_plug` source for washing machine, dryer,
  dishwasher, and kitchen diffuser plugs only.
- Keep non-household plug profiles such as H14, coffee machine, and bath
  diffuser on their existing direct sources.

## 0.1.20

- Prefer the new Core-Devices `sensor.benni_master_plug_power` facade for H14,
  appliance, and diffuser profile power bindings when it exists.
- Read per-device facade attributes such as `<device_id>_active` and
  `<device_id>_watt` before falling back to aggregate master attributes or raw
  watt thresholds.

## 0.1.19

- Keep Core-Devices master watt values and semantic active/idle hints separate.
  Plug Policy now displays the numeric `watt`/`power_w` value while using
  attributes like `is_active` only for activity classification, avoiding
  `active_state=active` with `power_w=null` in the UI.

## 0.1.18

- Rebind profile power-source candidates for PC, Denon, PS5, Switch, and TV
  plugs to the existing Core-Devices masters.
- Migrate saved `sensor.benni_device_*` power bindings to the corresponding
  `sensor.benni_master_*` source during ConfigEntry migration.
- Treat master boolean attributes (`is_active`, `powered`, `watt_active`,
  `protection_relevant`) as semantic active/idle input before falling back to
  watt attributes, so masters with `watt=0` can still protect active devices.

## 0.1.17

- Auto-suspend a device policy after repeated same-direction re-asserts to the same switch within the non-latch detection window, preventing slow relay churn that survives the command debounce.
- Resume an auto-suspended device only after the target state stays latched beyond the debounce interval.
- Add pure guard coverage plus coordinator regression coverage for repeated non-latching re-asserts.

## 0.1.16

- Generalize the hard 30 second command de-bounce from the kitchen diffuser plug to every managed switch.
- Keep opposite-direction commands immediate so real stop/sleep/away cuts are never delayed.

## 0.1.15

- Log kitchen diffuser command-cooldown skips at debug level instead of warning
  level so expected cooldown suppression does not appear as a Home Assistant
  integration error.

## 0.1.14

- Add a hard 30 second command cooldown for `switch.kitchen_diffuser_plug` so
  brief successful state reads cannot clear the pending-action guard and
  trigger a `turn_on` storm when the plug flaps back to `off`.

## 0.1.13

- Avoid removing the one-shot Home Assistant started listener after it has
  already fired, preventing a noisy restart/shutdown system-log error.

## 0.1.12

- Add a hard sleep guard for `bias_light` devices so entertainment/media context
  cannot turn the bias plug on while `bio=sleep`.
- Add regression coverage for the sleep-over-entertainment case before enabling
  persistent Apply mode.

## 0.1.11

- Make `apply_policy_now(device_id=...)` apply only the selected device instead
  of temporarily evaluating all devices with control enabled.
- Add regression coverage for targeted apply calls so observed single-device
  live tests stay scoped.

## 0.1.10

- Throttle duplicate in-flight `switch.turn_on/off` calls per device so fast
  state-change feedback loops cannot repeatedly fire the same plug action while
  Home Assistant has not yet reported the target switch state.
- Persist `set_enable_control` changes back to the Config Entry so Shadow/Live
  survives integration reloads and Home Assistant restarts.

## 0.1.9

- Replace missing saved profile power bindings, such as stale `_power_atomic`
  sensors, with known Core Devices sources during coordinator refresh.

## 0.1.8

- Add a coordinator-local power source fallback for known Einhornzentrale plug
  switches and expose `power_entity` source diagnostics on policy/summary
  sensors.

## 0.1.7

- Prefer direct `hass.states.get(entity_id)` checks when resolving profile
  entities, so coordinator startup/refresh can find Core Devices sensors even
  if `async_entity_ids()` is incomplete during setup.

## 0.1.6

- Resolve missing profile power entities during each coordinator refresh, so
  entries that start before Core Devices sensors are registered still pick up
  `sensor.benni_device_*` power once those states appear.

## 0.1.5

- Add a runtime coordinator fallback for known profile devices whose saved
  config still lacks `power_entity`, so existing entries can read Core Devices
  power immediately after restart even if storage migration did not persist the
  field.

## 0.1.4

- Migrate known Einhornzentrale plug power bindings from missing or old
  raw/atomic plug sensors to their Core Devices sources when available, while
  preserving unrelated custom power sensors.

## 0.1.3

- Bump the ConfigEntry `VERSION` to 3 so Home Assistant actually runs the
  `0.1.2` power-source migration on existing installations.

## 0.1.2

- Backfill known Benni profile plug devices with existing Core Devices or raw
  power sources when the saved `power_entity` is missing or unavailable.
- Read numeric power from Core Devices sensor attributes (`watt`, `power_w`,
  `power`) so `sensor.benni_device_*` can be used as policy power sources.

## 0.1.1

- Prefill global context selectors from `benni_core_devices`/Core State and
  `benni_media_state`.
- Migrate known legacy YAML and `benni_media_context` global source entity IDs
  to the current Core/Media-State entities.
- Fall back to current Core/Media-State globals when configured global sources
  are missing or unavailable.

## 0.1.0

- Extracted Plug Policy Engine from `bennis_toolbox` into the standalone
  `plug_policy_engine` Home Assistant integration.
- Kept `enable_control=false` as the safe default; policy decisions are exposed
  as entities before switch control is enabled.
- Added standalone config flow, services, storage helper, platform wrappers, and
  copied the current pure-engine and flow regression tests.
