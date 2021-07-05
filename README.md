![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)


This is a home assistant integration for a simple OCPP server (central system) for chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible repository 

## Installation

1. In home assistant, select Configuration / Integrations / Add Integration.
2. Search for 'OCPP' and add the integration.
3. Configure the Central System settings. The default port '0.0.0.0' will listen to all interfaces on your home assistant server.
5. Select which measurands you would like to become available as sensor entities.
6. After the integration has been added, click on devices and select the Charge Point device to see the device info.
7. From the device info you can add the entities to a lovelace dashboard.
9. Configure your charger to use the OCPP websocket (e.g. ws://homeassistant.local:9000 )

## Screenshot

![example](https://github.com/lbbrhzn/ocpp/raw/main/example.png "Example")
