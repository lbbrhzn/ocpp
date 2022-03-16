Debugging
=========

To enable debug logging for this integration and related libraries you
need to update your Home Assistant `configuration.yaml` file:

```
logger:
  default: info
  logs:
    custom_components.ocpp: debug
```
See [Home Assistant Logger](https://www.home-assistant.io/integrations/logger/)  for more info.

After a restart detailed log entries will appear in `/config/home-assistant.log`.
The log file can be displayed in your webbrowser, by selecting:

Configuration / Settings / Logs / LOAD FULL HOME ASSISTANT LOG

![LOAD FULL HOME ASSISTANT LOG](https://user-images.githubusercontent.com/8673442/158488329-64a2e38a-24d2-40ff-8743-643ebb337408.png)

You can filter for OCPP related messages by typing 'ocpp' in the 'search logs' box at the top of the page.

![search logs](https://user-images.githubusercontent.com/8673442/158488440-a5ae8076-5f33-49dd-86cb-7521cc74d96a.png)

A typical log for a working connection should look like this:

```
2022-03-16 16:33:08 INFO (MainThread) [custom_components.ocpp] {'host': '0.0.0.0', 'port': 9000, 'csid': 'central', 'cpid': 'pulsar', 'meter_interval': 60, 'idle_interval': 900, 'websocket_close_timeout': 10, 'WEBSOCKET_PING_TRIES': 2, 'websocket_ping_interval': 20, 'websocket_ping_timeout': 20, 'skip_schema_validation': False, 'monitored_variables': 'Energy.Active.Import.Register,Energy.Reactive.Import.Register,Energy.Active.Import.Interval,Energy.Reactive.Import.Interval,Power.Active.Import,Power.Reactive.Import,Power.Offered,Power.Factor,Current.Import,Current.Offered,Voltage,Frequency,RPM,SoC,Temperature,Current.Export,Energy.Active.Export.Register,Energy.Reactive.Export.Register,Energy.Active.Export.Interval,Energy.Reactive.Export.Interval,Power.Active.Export,Power.Reactive.Export'}
2022-03-16 16:35:40 INFO (MainThread) [custom_components.ocpp] Websocket Subprotocol matched: ocpp1.6
2022-03-16 16:35:40 INFO (MainThread) [custom_components.ocpp] Charger websocket path=/pulsar
2022-03-16 16:35:40 INFO (MainThread) [custom_components.ocpp] Charger pulsar connected to 0.0.0.0:9000.
2022-03-16 16:35:40 DEBUG (MainThread) [custom_components.ocpp] Received boot notification for pulsar: {'charge_point_serial_number': '88034', 'charge_point_vendor': 'Wall Box Chargers', 'meter_type': 'Internal NON compliant', 'meter_serial_number': '', 'charge_point_model': 'PLP1-0-2-4', 'iccid': '', 'charge_box_serial_number': '88034', 'firmware_version': '5.5.10', 'imsi': ''}
2022-03-16 16:35:40 DEBUG (MainThread) [custom_components.ocpp] Updating device info pulsar: {'charge_point_serial_number': '88034', 'charge_point_vendor': 'Wall Box Chargers', 'meter_type': 'Internal NON compliant', 'meter_serial_number': '', 'charge_point_model': 'PLP1-0-2-4', 'iccid': '', 'charge_box_serial_number': '88034', 'firmware_version': '5.5.10', 'imsi': ''}
2022-03-16 16:35:42 INFO (MainThread) [custom_components.ocpp] Supported feature profiles: Core,FirmwareManagement,LocalAuthListManagement,SmartCharging,RemoteTrigger
2022-03-16 16:35:42 INFO (MainThread) [custom_components.ocpp] Supported feature profiles: Core,FirmwareManagement,LocalAuthListManagement,SmartCharging,RemoteTrigger
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] Get Configuration for NumberOfConnectors: 1
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] Get Configuration for NumberOfConnectors: 1
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] Get Configuration for HeartbeatInterval: 3600
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] Get Configuration for HeartbeatInterval: 3600
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] 'pulsar' post connection setup completed successfully
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] trigger status notification for connector=0
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] 'pulsar' post connection setup completed successfully
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] trigger status notification for connector=0
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] trigger status notification for connector=1
2022-03-16 16:35:42 DEBUG (MainThread) [custom_components.ocpp] trigger status notification for connector=1
2022-03-16 16:36:00 DEBUG (MainThread) [custom_components.ocpp] Connection latency from 'central' to 'pulsar': ping=2.0 ms, pong=13.0 ms
2022-03-16 16:36:20 DEBUG (MainThread) [custom_components.ocpp] Connection latency from 'central' to 'pulsar': ping=2.0 ms, pong=9.0 ms
```

You can log even more information by enabling debug logging for websockets.server:

```
logger:
  default: info
  logs:
    websockets.server: debug
```

Filtering for websockets.server will yield this:

```
2022-03-16 16:33:08 INFO (MainThread) [websockets.server] server listening on 0.0.0.0:9000
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] = connection is CONNECTING
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < GET /pulsar HTTP/1.1
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Connection: Upgrade
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Host: homeassistant.fritz.box:9000
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Sec-WebSocket-Key: VLpFdctBQgYB6ZokyO2m3Q==
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Sec-WebSocket-Protocol: ocpp1.6
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Sec-WebSocket-Version: 13
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < Upgrade: websocket
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < User-Agent: WebSocket++/0.8.2
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > HTTP/1.1 101 Switching Protocols
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Upgrade: websocket
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Connection: Upgrade
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Sec-WebSocket-Accept: hLE0rT2uOtRgVH4VLWoK8K7McNU=
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Sec-WebSocket-Protocol: ocpp1.6
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Date: Wed, 16 Mar 2022 15:35:40 GMT
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > Server: Python/3.9 websockets/10.2
2022-03-16 16:35:40 INFO (MainThread) [websockets.server] connection open
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] = connection is OPEN
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"4a7920fe-1ded-48ff-b9c8-ff8f33bc8118","Boot...: "5.5.10","imsi": ""}]' [318 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"4a7920fe-1ded-48ff-b9c8-ff8f33bc8118",{"cur...0,"status":"Accepted"}]' [129 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"336a0acf-3117-4e72-99c6-f4ae31acb131","Stat...2022-03-16T15:35:40Z"}]' [211 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"336a0acf-3117-4e72-99c6-f4ae31acb131",{}]' [45 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"654f6701-639c-4398-8608-a0c7d8287465","Stat...2022-03-16T15:35:40Z"}]' [211 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"654f6701-639c-4398-8608-a0c7d8287465",{}]' [45 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"694f0dac-fad4-44e6-891c-23d535674cfd","Mete... 0,"transactionId": 0}]' [304 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"694f0dac-fad4-44e6-891c-23d535674cfd",{}]' [45 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"c2c18e7a-b6fc-40e4-ba5d-0423bf68d23d","Mete... 1,"transactionId": 0}]' [304 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"c2c18e7a-b6fc-40e4-ba5d-0423bf68d23d",{}]' [45 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] < TEXT '[2,"5191e2e7-f555-48b3-8b08-626679df5a80","Mete... 0,"transactionId": 0}]' [304 bytes]
2022-03-16 16:35:40 DEBUG (MainThread) [websockets.server] > TEXT '[3,"5191e2e7-f555-48b3-8b08-626679df5a80",{}]' [45 bytes]
```
