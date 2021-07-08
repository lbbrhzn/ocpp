![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

This is a home assistant integration for chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible repository 

## Installation


1. **Add the OCPP integration to HACS**
- If you have not yet installed HACS, go get it at https://hacs.xyz/ and walk through the installation and configuration.
- Go to the settings and add  this integration through the custom repositories:

![image](https://user-images.githubusercontent.com/13691266/124573829-2e42ad80-de4a-11eb-8ac7-5d141f237608.png)
- Add the url of this git repo: https://github.com/lbbrhzn/ocpp
- Restart Home Assistant!
- Install the new integration through *Configuration -> Integrations* in HA (see below).
2. **Add the OCPP integration**
- In Home Assistant, select Configuration / Integrations / Add Integration. Search for 'OCPP' and add the integration.
3. **Configure the Central System**
- The default port '0.0.0.0' will listen to all interfaces on your home assistant server.
- Select which measurands you would like to become available as sensor entities.
4. **Configure your charger**
- Configure your charger to use the OCPP websocket (e.g. ws://homeassistant.local:9000). This is charger specific, so consult your manual. 
5. **Add the entities to your dashboard**
- In Home Assistant, click on the the Charge Point device see the device info.
- Add the the entities to a lovelace dashboard using the button

## Screenshot

![example](https://github.com/lbbrhzn/ocpp/raw/main/example.png "Example")

## Supported devices

The following devices are supported :
OCPP 1.6 compatible devices 


## Development
### Debugging

To enable debug logging for this integration and related libraries you
can control this in your Home Assistant `configuration.yaml`
file. Example:

```
logger:
  default: info
  logs:
    custom_components.ocpp: critical
    websocket: debug
```

After a restart detailed log entries will appear in `/config/home-assistant.log`.


