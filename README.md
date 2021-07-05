![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

This is a home assistant integration for chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible repository 

## Installation

1. **Add the OCPP integration**
- In Home Assistant, select Configuration / Integrations / Add Integration. Search for 'OCPP' and add the integration.
2. **Configure the Central System**
- The default port '0.0.0.0' will listen to all interfaces on your home assistant server.
- Select which measurands you would like to become available as sensor entities.
3. **Configure your charger**
- Configure your charger to use the OCPP websocket (e.g. ws://homeassistant.local:9000). This is charger specific, so consult your manual. 
4. **Add the entities to your dashboard**
- In Home Assistant, click on the the Charge Point device see the device info.
- Add the the entities to a lovelace dashboard using the button

## Screenshot

![example](https://github.com/lbbrhzn/ocpp/raw/main/example.png "Example")
