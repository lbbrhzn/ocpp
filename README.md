# OCPP

This is a home assistant integration for a simple OCPP server (central system) for chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible respository 

## Installation

* install HACS
* add this repository
* install the ocpp integration
* add the platform to configuration.yaml

## Example configuration

---
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
---    

## Screenshot

![example](example.png "Example")

