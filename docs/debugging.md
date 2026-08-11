Debugging
=========

To enable debug logging for this integration and related libraries you
need to update your Home Assistant `configuration.yaml` file:

```yaml
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

```text
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

To debug issues with establishing the ocpp connection, you can enable debug logging for websockets.server:

```yaml
logger:
  default: info
  logs:
    websockets.server: debug
```

Filtering for websockets.server should yield something like this:

```text
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

No Charge Control switch, and the charger cannot be started
-----------------------------------------------------------

**Symptom:** the charger's connector entities are missing — most visibly
`switch.<cpid>_charge_control`, so charging cannot be started or stopped from
Home Assistant. In *Developer tools / States* the entity either does not appear
at all, or appears as `unavailable` with a `restored: true` attribute (meaning
it is a leftover registry entry that the integration is not creating). There is
no error in the log. If the charger cannot complete its connection setup —
because it is offline, or failing partway through setup — then restarting Home
Assistant, reloading the integration and switching the charger between OCPP 1.6
and 2.0.1 all appear to change nothing, because the count is stored in the
config entry rather than held in runtime state.

**Cause:** the connector count stored in the config entry is `0`. The
per-connector entities are created from that number, so a stored `0` creates
none of them. Chargers whose OCPP 2.0.1 `GetBaseReport` inventory reports no
`Connector` components could produce a `0` on earlier versions, and because
the value is written into the config entry it survives restarts and updates.

**Check it** — in the *Terminal* add-on:

```bash
python3 -c "import json;d=json.load(open('/config/.storage/core.config_entries'));[print(k,v.get('num_connectors')) for e in d['data']['entries'] if e['domain']=='ocpp' for c in e['data'].get('cpids',[]) for k,v in c.items()]"
```

A charger reporting `0` is affected.

**Fix it.** Current versions correct the value automatically the next time a
charger connects and completes its setup, so reconnecting the charger is
usually enough. If the charger cannot complete setup, removing the
integration and adding it back also clears it.

Once the connector entities are recreated, the session sensors regain their
units and Home Assistant may raise a one-time `units_changed` repair for
`sensor.<cpid>_time_session`. Choose **"Update the unit of the historic
statistic values, without converting"** to keep the existing history: the values
were always minutes, only the unit label was missing while the connector slots
were uninitialised.
