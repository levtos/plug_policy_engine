# Plug Policy Engine

Native Home-Assistant-Integration zur Verwaltung schaltbarer Steckdosen nach
dokumentierten Policies. Ersetzt YAML-Duplikate für Always-On, Home-Baseline,
Away-Cut, Schedule-Context, Charging-Safe und Sonderlogiken.

Decision-Engine ist reines Python und wird ohne Home Assistant getestet.

## Installation (HACS, Custom Repository)

1. HACS → Integrations → ⋮ → Custom repositories → diese Repo-URL hinzufügen
   (Category: Integration).
2. „Plug Policy Engine" installieren, Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → „Plug Policy Engine".

Im Setup wählst du die globalen Selektoren (Presence, Bio, Day, Media,
Entertainment, optional Activity) und entscheidest, ob die Integration nur
**Sensoren** liefern oder **direkt schalten** darf (`enable_control`).

Neue Setups werden auf Benni automatisch mit den aktuellen Core-/Media-Quellen
vorbelegt:

- Presence: `sensor.benni_combined_context_presence_personal`
- Bio: `sensor.benni_combined_context_bio_state`
- Day: `sensor.benni_combined_context_day_state`
- Media: `sensor.benni_media_state_media_context`
- Gaming Source: `sensor.benni_media_state_gaming_source`
- Entertainment: `binary_sensor.benni_media_state_entertainment_active`
- Activity: `sensor.benni_combined_context_activity_state`

Feine Core-Day-States wie `afternoon` oder `late_night` werden intern auf die
Plug-Policy-Kontexte `day` bzw. `night` normalisiert.

Geräte fügst du danach im Optionsmenü hinzu, einzeln.

Die Integration bleibt absichtlich unter der Domain `plug_policy_engine`.
Der Slug ist Teil bestehender Config Entries, Services, Entities und Panel-URLs
und wird deshalb nicht an das `benni_*`-Namensschema angepasst.

## Policy-Tabelle

| Kürzel | Name             | Verhalten                                                                       |
|--------|------------------|---------------------------------------------------------------------------------|
| AO     | Always On        | Immer an; nach HA-Neustart einschalten; nie auto-off.                           |
| HB     | Home Baseline    | Standard-Zuhause-Verhalten; schaltet nicht automatisch aus.                     |
| AC     | Away Cut         | Schaltet bei echter `abwesend` aus, sobald das Gerät stabil idle ist.           |
| SC     | Schedule Context | An nur in `allowed_contexts` (z. B. Tagesphasen) + awake + zuhause.             |
| CS     | Charging Safe    | Funktional ≈ AO; nie hart auto-off.                                             |
| SPECIAL| Sonderlogik      | Wird über `kind` (z. B. tablet, diffuser, bias_light) bestimmt.                 |

## Spezielle Gerätetypen (`kind`)

- **pc** — aktiv → niemals auto-off. Idle + Bio=sleep → off. `set_manual_recently_on`
  setzt einen Cooldown (Standard 15 min), in dem auto-off blockiert ist.
- **appliance** (Wasch-/Trockner/Spülmaschine) — laufende Programme werden nie
  unterbrochen. Power=unknown → geschützt.
- **coffee_maker** — AO + `wake_signal_only`: dient nur als Wake-Indiz, wird
  niemals automatisch geschaltet.
- **bias_light** — folgt direkt dem TV-Master: TV an → Bias an, TV aus → Bias aus.
  `bio=sleep` erzwingt aus; nach `awake` folgt das Bias-Light wieder dem TV. Bei
  unbekanntem TV-Master wird der bestehende Schaltzustand gehalten.
- **diffuser** — SC mit 15/15-Takt; läuft nur awake + zuhause + erlaubte Phase.
  Stoppt sofort bei sleep, away oder night.
- **tablet** — 40/80-Ladelogik 24/7 unabhängig von Presence/Bio. Unter 20% gilt
  absolute Tiefentladungs-Priorität.
- **denon / h14_dock / generic** — folgen ihrer Policy ohne Sonderzweige.

## Outputs

Globale Entities:

- `sensor.plug_policy_engine_summary` — Übersicht aller Entscheidungen
- `binary_sensor.plug_policy_engine_any_blocked` — wahr, wenn mind. ein Gerät einen Blocker hat

Pro konfiguriertem Gerät:

- `binary_sensor.<device>_active`
- `sensor.<device>_plug_policy_state`
- `sensor.<device>_plug_decision`  (Attribute: policy, desired_switch_state,
  reason, blockers, power_w, active_state, context, last_action)
- `sensor.<device>_last_policy_action`

## Observability-Panel und WebSocket

Das Sidebar-Panel `Plug Policy` nutzt den administrativen WebSocket-Command
`plug_policy_engine/get_status`. Der Contract liefert:

- `global`: `enable_control`, aktueller Kontext und `last_update_ts`
- `devices[]`: `device_id`, Name, Kind, Policy, Ist-Zustand, Soll-Zustand,
  Grund, Blocker, Thresholds, `stable_off_remaining_s`, Allowed Contexts,
  Suspension-Status, Kontext-Snapshot und Kind-Widget-Daten
- `debug_export`: vollständiger Snapshot fuer Diagnose/Trace-Weiterverarbeitung

Das Panel zeigt Shadow/Live-Gate, Kontextstreifen, Device-Grid, Detail/Trace,
Diagnose-Kette und JSON-Debug-Export.

## Services

- `plug_policy_engine.force_evaluate`
- `plug_policy_engine.apply_policy_now` (optional `device_id`; mit
  `device_id` wird nur dieses eine Gerät temporär angewendet)
- `plug_policy_engine.set_enable_control` (`enabled`) — Shadow/Live-Gate setzen
- `plug_policy_engine.suspend_device_policy` (`device_id`)
- `plug_policy_engine.resume_device_policy` (`device_id`)
- `plug_policy_engine.set_manual_recently_on` (`device_id`) — PC-Cooldown setzen

## Beispiel — PC mit Manual-On-Guard

```yaml
# devices (vereinfacht):
- name: Schreibtisch PC
  switch_entity: switch.desk_pc_plug
  power_entity: sensor.desk_pc_power
  policy: HB
  kind: pc
  active_threshold: 30
  idle_threshold: 8
  unknown_behavior: assume_active
  never_cut_when_active: true
  manual_on_cooldown_seconds: 900
```

Automatisierung: wenn der Nutzer den PC manuell einschaltet, ruf
`plug_policy_engine.set_manual_recently_on` mit `device_id: <…>` — auto-off
ist dann für 15 Minuten blockiert.

## Beispiel — Tablet 40/80

```yaml
- name: Tablet Wohnzimmer
  switch_entity: switch.tablet_charger
  battery_entity: sensor.tablet_battery_level
  policy: SPECIAL
  kind: tablet
  tablet_low: 40
  tablet_high: 80
```

## Beispiel — Diffuser

```yaml
- name: Duftstecker Wohnzimmer
  switch_entity: switch.diffuser
  policy: SC
  kind: diffuser
  allowed_contexts: [day, evening]
  diffuser_on_minutes: 15
  diffuser_off_minutes: 15
```

## Tests

```
pytest tests/
```

Die Decision-Engine läuft ohne Home-Assistant-Mock. Abgedeckt:

- `bei_eltern` löst keine Away-Cuts aus
- `abwesend` + idle appliance → off; aktiv → no action
- power unavailable → geschützt
- AO/CS off bei HA-Start → on
- PC aktiv → niemals off; PC idle + sleep → off; PC manual-on cooldown
- Tablet <40 → on; ≥80 → off; unavailable → on; <20 % deep-discharge
- Bias Light folgt ausschließlich TV-Master und Sleep-Override; unbekannter TV hält
- Diffuser stoppt bei sleep / away / night und folgt 15/15-Zyklus
- SPECIAL & suspend halten an

## Sicherheit & Idempotenz

- Schalt-Calls (`switch.turn_on/off`) werden nur abgesetzt, wenn der aktuelle
  Schalterzustand vom gewünschten abweicht.
- Gleichartige In-Flight-Schalt-Calls werden pro Gerät gedrosselt, bis Home
  Assistant den Zielzustand zurückmeldet oder ein kurzer Retry-Guard abläuft.
- `plug_policy_engine.apply_policy_now(device_id=...)` ist auf das ausgewählte
  Gerät begrenzt; ohne `device_id` wird bewusst die gesamte Policy angewendet.
- `plug_policy_engine.set_enable_control` persistiert Shadow/Live in der
  Config Entry, damit der Gate-Zustand Reloads und Neustarts überlebt.
- Ohne `enable_control` liefert die Integration ausschließlich Entscheidungs-Sensoren.
- Keine blocking I/O im Eventloop.
