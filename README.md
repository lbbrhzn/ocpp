# OCPP

This is a home assistant integration for a simple OCPP server (central system) for chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible repository 

## Installation

1. Install [HACS](https://hacs.xyz/) in home assistant
2. Add this repository as a Custom repository in HACS 
3. Search for the ocpp integration and install
4. Add the ocpp platform configuration (see below) to configuration.yaml
5. Configure your charger to use the OCPP websocket (e.g. ws://homeassistant.local:9000 )
6. Restart Home Assistant
7. Update your home assistant dashboard to include the new OCPP Entities

## Example configuration

```yaml
sensor:
  - platform: ocpp
    monitored_variables:
      - "Current.Export"
      - "Current.Import"
      - "Current.Offered"
      - "Energy.Active.Export.Register"
      - "Energy.Active.Import.Register"
      - "Energy.Reactive.Export.Register"
      - "Energy.Reactive.Import.Register"
      - "Energy.Active.Export.Interval"
      - "Energy.Active.Import.Interval"
      - "Energy.Reactive.Export.Interval"
      - "Energy.Reactive.Import.Interval"
      - "Frequency"
      - "Heartbeat"
      - "Power.Active.Export"
      - "Power.Active.Import"
      - "Power.Factor"
      - "Power.Offered"
      - "Power.Reactive.Export"
      - "Power.Reactive.Import"
      - "RPM"
      - "SoC"
      - "Status"
      - "Temperature"
      - "Voltage"
    port: 9000
    scan_interval:
      seconds: 60
```

## Example dashboard
```yaml
views:
  - title: Charging
    path: charging
    badges: []
    cards:
      - type: history-graph
        entities:
          - entity: sensor.energy_active_import_register
          - entity: sensor.current_import
          - entity: sensor.status
        hours_to_show: 24
        refresh_interval: 0
      - type: entities
        entities:
          - entity: sensor.status
          - entity: sensor.energy_active_import_register
          - entity: sensor.energy_reactive_import_register
          - entity: sensor.power_active_import
          - entity: sensor.power_reactive_import
          - entity: sensor.current_offered
          - entity: sensor.current_import
          - entity: sensor.heartbeat
          - entity: sensor.soc
        title: OCPP
```

## Screenshot

![example](example.png "Example")

