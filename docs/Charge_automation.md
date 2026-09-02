# Dynamically Adjusting EV Charge Current: A Smart Approach

Dynamically adjusting the charge current of an electric vehicle (EV) within a home automation system offers significant advantages:

* **Preventing Overload:**
    * By monitoring real-time energy consumption, you can automatically reduce the EV's charging rate to prevent overloading the household's electrical circuits and potentially tripping the main fuse.

* **Optimizing Solar Power Usage:**
    * When the solar panel production is available in Home Assistant, you can prioritize charging the EV with excess solar energy.

* **Demand Response:**
    * When you have dynamic energy pricing, you can adjust charging rates based on time-of-use electricity pricing.

This page provides several examples and hints to illustrate some of the many potential use cases. They are examples to adapt, not a finished installation: the values that depend on your supply and your charger are called out as you go.

## Entity names used on this page

`<cpid>` is the charge point id you configured for your charger. The examples use the entity names of a **single-connector** charger.

If your charger reports more than one connector, those charger-level entities do not exist — the integration creates per-connector entities instead and removes the flat ones. Use the connector that matches the `conn_id` you pass to the action:

| Single connector | Two or more connectors |
| --- | --- |
| `sensor.<cpid>_transaction_id` | `sensor.<cpid>_connector_1_transaction_id` |
| `sensor.<cpid>_current_import` | `sensor.<cpid>_connector_1_current_import` |
| `number.<cpid>_maximum_current` | `number.<cpid>_connector_1_maximum_current` |

## Adjusting the charge current

When the OCPP integration is added to your Home Assistant, you get a slider to control the maximum charge current named:
`number.<cpid>_maximum_current`

While using this entity in an automation might seem logical, do not assume it is safe to update at control-loop frequency.
This entity controls the OCPP ChargePointMaxProfile, which configures the maximum power or current available for the entire charging station.
OCPP defines the profile's behaviour, but not where a charger stores it. Some charger firmware persists station-wide profiles in non-volatile memory; other firmware does not. If a charger writes every update to EEPROM or flash, a fast control loop can wear that storage. There is no universal safe update rate or lifetime estimate: confirm the implementation and supported update frequency with the charger manufacturer.

⚠️ **Warning**: Do not use the maximum-current slider as a high-frequency control loop unless the charger manufacturer confirms that repeated profile updates are safe. Debounce ordinary changes, require a meaningful change before sending another profile, and retain the ability to send an immediate safety reduction.

### TxProfile

For session-scoped control, use a profile that is active exclusively during the current charging session. This allows you to adjust the charge current downwards while still respecting the upper limit defined by the ChargePointMaxProfile.

Essentially, the slider in your GUI maintains control over the absolute maximum current the charger can utilize.

### What `ocpp.set_charge_rate` does with `limit_amps`

Before writing a profile by hand it is worth knowing what the built-in action does, because it is not simply a session-scoped limit.

On **OCPP 1.6** it first tries a station-wide `ChargePointMaxProfile` on connector 0, the same profile purpose the slider writes. **If the charger accepts it, that is the end of the call.** Only if the charger *rejects* it does the action fall back to a `TxProfile` bound to the running transaction, plus a `TxDefaultProfile` for later sessions. Which of those happened depends on your charger, and the action reports success either way.

So on a charger that accepts the station-wide profile, calling this on a short interval repeatedly exercises whatever storage path that firmware uses, and the limit can outlive the transaction rather than being session-scoped.

On **OCPP 2.0.1** a limit below the maximum writes a `ChargingStationMaxProfile` to EVSE 0, as the specification requires for a station-wide profile. A limit at or above the configured maximum current, or 22 kW for a watt limit, or a call with no limit at all, clears that profile instead of sending one. There is no TxProfile fallback, and `conn_id` is not used for this managed limit — it only selects the EVSE for a `custom_profile`.

```yaml
- action: ocpp.set_charge_rate
  data:
    devid: <cpid>
    conn_id: 0
    limit_amps: 16
```

It remains the right tool for an occasional limit change, and it handles the details below for you. A hand-written `TxProfile` gives session scope, but OCPP does **not** guarantee that a charger keeps it out of non-volatile storage. Confirm the charger's behaviour before updating any profile frequently.

### Writing your own profile

Everything below sends a raw profile through `custom_profile`, exactly as you write it. Pass `custom_profile` as a mapping, not as a JSON string under `custom_profile: |`. Keeping the profile structured preserves its field types and avoids reparsing charger-originated values as JSON.

That escape hatch hands you three things the managed path was handling:

* **The profile id.** A charger replaces an existing profile that has the same id. On OCPP 1.6 the integration uses `1000` for ChargePointMaxProfile, `2000+n` for TxDefaultProfile and `3000+n` for TxProfile, where `n` is the connector. On 2.0.1 it uses id `1` for the `ChargingStationMaxProfile` the slider writes. **Reusing `1` on 2.0.1 can replace the slider's ceiling with your session profile, which then disappears when the transaction ends.** Maintain your own charger-wide inventory and use a different integer for every connector and purpose.
* **The stack level.** Among overlapping profiles of the same purpose, a higher supported level takes precedence. A profile can therefore be accepted but have no effect when another profile is above it. Do not assume that level 2 is valid or sufficient. On OCPP 1.6, query `ChargeProfileMaxStackLevel`; on 2.0.1, inspect the charger's smart-charging device-model variables. Coordinate the chosen level with every other system that writes profiles, and keep it within the maximum the charger reports.
* **The rate unit and phase count.** See below.

The examples take the ids and stack level as variables. The placeholders below are deliberately non-numeric, so an unedited snippet is rejected by schema validation instead of silently installing a colliding default. Replace them with integer values chosen for your charger. On OCPP 1.6, remember that the resulting rejection is visible only in the integration log and notification because the action itself still returns normally.

```yaml
variables:
  # Replace these with different charger-wide integer ids.
  tx_profile_id: <unique_integer_for_this_connector_and_purpose>
  tx_default_profile_id: <different_unique_integer_for_this_connector_and_purpose>
  # Replace this with an integer the charger supports and that is
  # coordinated with every other profile writer.
  stack_level: <supported_integer_stack_level>
```

Two more things apply to every action example below:

* **Always pass `devid`.** Without it the action falls back to whichever charger happens to be first in the integration's internal ordering, which is not necessarily the one your template just read; with more than one central system configured, an omitted `devid` fails the call outright.
* **The profile shape differs between OCPP 1.6 and 2.0.1.** The two are not interchangeable. After replacing the deliberately invalid integer placeholders, both shapes below validate against the OCPP schemas the integration uses.

The snippets show only the action. `charge_current` is whatever your automation calculated - see [Designing your automation](#designing-your-automation) for the decisions that go into it.

#### OCPP 1.6

```yaml
- action: ocpp.set_charge_rate
  data:
    devid: <cpid>
    conn_id: 1
    custom_profile:
      transactionId: "{{ states('sensor.<cpid>_transaction_id') | int }}"
      chargingProfileId: "{{ tx_profile_id }}"
      stackLevel: "{{ stack_level }}"
      chargingProfilePurpose: TxProfile
      chargingProfileKind: Relative
      chargingSchedule:
        chargingRateUnit: A
        chargingSchedulePeriod:
          - startPeriod: 0
            limit: "{{ charge_current | float }}"
```

#### OCPP 2.0.1

On 2.0.1 the profile id field is `id` (not `chargingProfileId`), `chargingSchedule` is a **list**, and `transactionId` is a **string** — a number there is rejected by the schema before anything reaches the charger.

Home Assistant normally converts a template containing only `"0"` to the number `0`. Because `"0"` is a valid string transaction id on 2.0.1, this example renders the complete mapping in one expression. That preserves the transaction id as a string while still delivering a dictionary, rather than a JSON string, to the integration. The charger-originated id is a dictionary value and cannot add or replace profile fields.

```yaml
- action: ocpp.set_charge_rate
  data:
    devid: <cpid>
    conn_id: 1
    custom_profile: >-
      {{ {
        'id': tx_profile_id,
        'stackLevel': stack_level,
        'chargingProfilePurpose': 'TxProfile',
        'chargingProfileKind': 'Relative',
        'transactionId': states('sensor.<cpid>_transaction_id'),
        'chargingSchedule': [{
          'id': 1,
          'chargingRateUnit': 'A',
          'chargingSchedulePeriod': [{
            'startPeriod': 0,
            'limit': charge_current | float
          }]
        }]
      } }}
```

#### Check which rate unit your charger accepts

The profiles above request a limit in amps (`"chargingRateUnit": "A"`). Not every charger accepts that: some are power-only and want watts. The integration negotiates this for the limits it sends itself, but a **custom profile is forwarded exactly as you wrote it**, so an unsupported unit is simply rejected.

Ask the charger rather than assuming. `ocpp.get_configuration` answers with a response rather than changing anything, so the easiest place to run it is **Developer Tools → Actions**, which shows you the result. In an automation a response-only action must be given a `response_variable`, or Home Assistant refuses it:

```yaml
- action: ocpp.get_configuration
  data:
    devid: <cpid>
    ocpp_key: ChargingScheduleAllowedChargingRateUnit
  response_variable: allowed_units
```

That configuration key is **OCPP 1.6**. On 2.0.1 the same question is asked of the device model, and `ocpp_key` must be a `Component/Variable` path — the smart charging controller is `SmartChargingCtrlr` — so check your charger's device model for the variable it exposes. A key without a `/` is rejected on 2.0.1.

If the charger reports power only, use `"chargingRateUnit": "W"` and convert your figure using the same voltage and phase count as below: watts = amps × voltage × phases.

Say the phase count in the profile as well, with `"numberPhases": 1` (or 3) in the schedule period. Converting for one phase and then omitting the field does not tell the charger anything: an AC station that sees no `numberPhases` may assume three, divide your watts across them, and end up below the minimum current it will charge at.

#### Only send a TxProfile when there is a transaction

A `TxProfile` is bound to the running transaction. If the transaction sensor is `unknown` or `unavailable` — which happens around restarts and telemetry hiccups — there is nothing to bind to, and a profile sent anyway is rejected by the charger.

That failure is quiet: on OCPP 1.6 the integration logs the rejection, but the action still returns normally, so an automation cannot tell that its limit was refused. Check for a transaction before sending, rather than letting a template substitute a placeholder value.

On **OCPP 1.6** transaction ids are numbers and the integration writes `0` when a transaction ends, so zero means "no session":

```yaml
condition:
  - condition: template
    value_template: >-
      {{ states('sensor.<cpid>_transaction_id') | int(0) > 0 }}
```

On **OCPP 2.0.1** the id is an arbitrary string chosen by the charger, and `"0"` is a perfectly valid one — excluding it would silence every update for that session. Check only that there is a value:

```yaml
condition:
  - condition: template
    value_template: >-
      {{ has_value('sensor.<cpid>_transaction_id')
         and states('sensor.<cpid>_transaction_id') not in ['None', ''] }}
```

### Limiting future sessions

`transactionId` only means something for a `TxProfile`. To limit **future** sessions, send a `TxDefaultProfile` with no transaction binding at all — do not take the profile above and change only the purpose, as the leftover `transactionId` can get the profile rejected:

```yaml
- action: ocpp.set_charge_rate
  data:
    devid: <cpid>
    conn_id: 1
    custom_profile:
      chargingProfileId: "{{ tx_default_profile_id }}"
      stackLevel: "{{ stack_level }}"
      chargingProfilePurpose: TxDefaultProfile
      chargingProfileKind: Relative
      chargingSchedule:
        chargingRateUnit: A
        chargingSchedulePeriod:
          - startPeriod: 0
            limit: "{{ charge_current | float }}"
```

For OCPP 2.0.1, use the 2.0.1 shape above with `"chargingProfilePurpose": "TxDefaultProfile"` and the `transactionId` line removed.

## Units, voltage and phases

The examples below convert power to a current, so they depend on three things you must set to match your installation:

* **Units.** A power sensor may report W or kW. Check which, and convert: the examples note the unit they expect.
* **Voltage.** The voltage the *charger* is fed at, which is not always what your meter reports per phase. On a line-to-neutral supply it is the phase voltage, e.g. 230 V in much of the EU. On a North American split-phase circuit the charger sits across two legs, so it sees 240 V even though each leg measures 120 V to neutral.
* **Phases.** A charge current limit is **per phase**. On a balanced three-phase supply, a total power of P watts corresponds to `P / (3 × voltage)` amps per phase, not `P / voltage`.

There are two easy ways to get this wrong, and both ask for more current than you meant:

* Treating a three-phase supply as single-phase: 4600 W becomes `4600 / 230 = 20 A` instead of `4600 / (3 × 230) ≈ 6.7 A` — three times too much.
* Using the line-to-neutral voltage on a split-phase circuit: a 7.2 kW charger becomes `7200 / 120 = 60 A` instead of `7200 / 240 = 30 A` — twice too much.

Set `voltage` and `phases` in the examples below to match how your charger is actually fed: 230 and 1 for a single-phase EU supply, 230 and 3 for three-phase, 240 and 1 for North American split-phase. These formulas assume a balanced supply; if yours is not, measure the phase you are actually limiting.

## Solar current

The solar system usually reports its production in Watts or kW. To convert this to amps available for your EV charger, divide the watts by the line voltage and by the number of phases.

You can create a template sensor for this. The `availability` template is what keeps a missing input from being read as a genuine zero — the sensor goes unavailable instead of quietly reporting 0 A:

```yaml
template:
  - sensor:
      - name: "Solar charge current"
        unique_id: solar_charge_current
        unit_of_measurement: "A"
        device_class: current
        state_class: measurement
        # is_number rejects unknown, unavailable and a meter that reports
        # something like "error"; the age check catches one that has
        # simply stopped publishing while still looking available.
        availability: >-
          {{ is_number(states('sensor.solaredge_power'))
             and (now() - states.sensor.solaredge_power.last_reported).total_seconds() < 900 }}
        state: >-
          {% set phases = 3 %}
          {% set voltage = 230 %}
          {% set solar_w = states('sensor.solaredge_power') | float(0) %}
          {{ (solar_w / (voltage * phases)) | round(0, 'floor') }}
```

This sensor contains the solar current in amps per phase, rounded down so it never asks for more than is actually available.

## Smart-meter

The paragraph above suggests that nearly all the solar current is prioritized for the EV charger. However, this can lead to situations where other high-demand appliances, such as a washing machine or hot tub, still draw power from the grid even when solar energy is available.

To avoid this, you can use the data provided by your smart meter sensors. By integrating smart meter data into your home automation system, you can dynamically adjust the EV charging rate based on real-time energy consumption. This ensures that the EV primarily charges using excess solar power while minimizing reliance on grid electricity during periods of high household demand.

For this it is important to know the current you deliver or receive from the grid. Depending on your smart meter sensors you might have this current available in a sensor, or you might have it as power. The example below uses power to and from the grid (in kW) and converts it to a current which is negative when receiving from the grid.

Every input is checked in `availability`, not just defaulted in `state`. `is_number` is used rather than `has_value` because a meter can stay "available" while publishing something that is not a number at all. Each input carries its own `last_reported` age because they can freeze independently: an export reading stuck at 5 kW while import keeps ticking looks entirely healthy if you timestamp only one of them. `last_reported` is used instead of `last_updated` so repeated reports of the same valid value count as fresh.

This example assumes the meter reports at least every 10 seconds. It allows 30 seconds without a report and runs a watchdog every 10 seconds, so a frozen input becomes unavailable less than 40 seconds after its last report. Change both figures to match the real reporting interval, allowing only a small number of missed samples. The state triggers react immediately to changed readings; the watchdog also catches a sensor that simply stops publishing. A renamed or dropped import sensor would otherwise read as `0 kW` — indistinguishable from a house drawing nothing — and the correction would quietly stop working. The voltage is range-checked too: a meter that publishes a literal `0 V` while restarting converts perfectly well to the number zero, so `| float(230)` would not save you from dividing by it.

```yaml
template:
  - triggers:
      - trigger: state
        entity_id:
          - sensor.p1_meter_p1_voltage
          - sensor.p1_meter_p1_returned
          - sensor.p1_meter_p1_power
      - trigger: time_pattern
        seconds: "/10"
    sensor:
      # The current still available to use; negative means you are
      # already importing from the grid.
      - name: "Grid current available"
        unique_id: grid_current_available
        unit_of_measurement: "A"
        device_class: current
        state_class: measurement
        availability: >-
          {% set max_age_seconds = 30 %}
          {{ is_number(states('sensor.p1_meter_p1_voltage'))
             and is_number(states('sensor.p1_meter_p1_returned'))
             and is_number(states('sensor.p1_meter_p1_power'))
             and states('sensor.p1_meter_p1_voltage') | float(0) > 100
             and states('sensor.p1_meter_p1_voltage') | float(0) < 300
             and (now() - states.sensor.p1_meter_p1_voltage.last_reported).total_seconds() < max_age_seconds
             and (now() - states.sensor.p1_meter_p1_returned.last_reported).total_seconds() < max_age_seconds
             and (now() - states.sensor.p1_meter_p1_power.last_reported).total_seconds() < max_age_seconds }}
        state: >-
          {% set phases = 3 %}
          {% set voltage = states('sensor.p1_meter_p1_voltage') | float(230) %}
          {% set exported_kw = states('sensor.p1_meter_p1_returned') | float(0) %}
          {% set imported_kw = states('sensor.p1_meter_p1_power') | float(0) %}
          {{ ((exported_kw - imported_kw) * 1000 / (voltage * phases)) | round(1) }}
```

A positive value means you can increase the EV charge current by that much; a negative value means you need to decrease it.

(designing-your-automation)=
## Designing your automation

The pieces above are deliberately building blocks rather than a finished control loop. What to do with the numbers depends on your supply, your charger and your tolerance for getting it wrong, and the details that decide whether such an automation is safe cannot be captured in a snippet. Before you wire one up, work through these:

* **Fail safe, not open.** Decide what happens when a sensor is unavailable. Falling back to "leave the limit where it was" is the wrong direction for overload protection: the moment you cannot see the grid is the moment to be cautious. Send a current your supply can always carry instead, and make sure every input the calculation uses is checked — including the charger's own current sensor, which is just as capable of going unavailable as the meter is.

* **Do not let a safety update be dropped.** An automation triggered only by the meter never runs when the meter stops updating, and `mode: single` discards a new trigger while a previous run is still waiting on the charger. Trigger when the derived safety sensor becomes unavailable, add a periodic watchdog, trigger on the transaction as well as the meter, choose the mode deliberately, and test what happens when the charger is slow to answer.

* **Control retries and update volume.** Coalesce superseded increases, allow only one profile write to be in flight, and use a minimum change and a minimum interval for ordinary adjustments. Do not delay an overload reduction behind that interval. After a timeout, use bounded retries with backoff and jitter; an unbounded retry loop can keep a charger and Home Assistant busy throughout an outage while still leaving the old limit active.

* **Clamp to what the installation allows.** Most chargers refuse to charge below about 6 A, so a calculated 4 A is not a gentle reduction — it is a rejected profile and the previous limit staying in force. Cap the upper end at the supply's per-phase rating too, and decide explicitly whether "below the minimum" means suspend the session or leave it at the minimum.

* **A rejected profile is not visible on OCPP 1.6.** The integration logs it, but the action returns normally, so your automation cannot tell. Treat a sent profile as a request, not a confirmation. Watching `sensor.<cpid>_current_import` afterwards can tell you a limit is being *exceeded*, but it cannot confirm one was accepted: a car that is tapering, paused, or simply charging slowly sits below the limit whether or not the charger ever installed it.

* **Match the protocol version.** The two profile shapes above are not interchangeable, and neither is the transaction id's type.

## Maximum charge

A similar solution could be used to check how much power is still available from the grid, subtracting all power used by other appliances in your house. This way you can charge your EV as fast as possible without overloading your main fuse.

Whatever you compute, clamp the result to what your installation and charger actually allow — the supply's per-phase rating and the charger's own minimum and maximum current. Most chargers will not charge below about 6 A.

How long a supply tolerates a given overload before its protection opens depends on the device — fuse class, breaker curve, ambient temperature, conductor and termination ratings — and on local rules, so there is no universal figure to design against. Treat the rated current as the limit for normal operation rather than something to exceed briefly, and take any timing assumption from the data sheet of the device actually installed, or from a qualified electrician.

That matters more than it sounds: if the protection does open, it takes Home Assistant, your network and the charger with it, so nothing is left to correct the overload.
