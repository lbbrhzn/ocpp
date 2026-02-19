Installation
============


## Install HACS
- If you have not yet installed HACS, go get it at [https://hacs.xyz](https://hacs.xyz/) and walk through the installation and configuration.

## Install the OCPP Repository
- In Home Assistant, select HACS / Integrations / + Explore & add repositories.

![image](https://user-images.githubusercontent.com/8673442/129494626-6e7a82b3-659f-4c39-a7be-43f70141cc7b.png)
- Search for 'OCPP' and install the repository.

## Add the OCPP Integration
- In Home Assistant, select Configuration / Integrations / Add Integration.

![image](https://user-images.githubusercontent.com/8673442/129494673-4718ba88-7872-435b-a331-66c8c34dddeb.png)
- Search for 'OCPP' and add the integration.

![image](https://user-images.githubusercontent.com/8673442/129494723-80e2e402-7564-4e86-b599-b87f32987ac0.png)

## Configure the Central System
### Host address and port
- The default host address '0.0.0.0' will listen to all interfaces on your home assistant server.
- The default port number is 9000 but can be changed for your needs.
<img width="580" src="https://user-images.githubusercontent.com/25015949/229121761-6a0f4a71-9282-4c44-a06d-cecdc2f832da.png">


### Secure Connection
If you are using [Let’s Encrypt](https://github.com/home-assistant/addons/tree/master/letsencrypt), [Duck DNS](https://www.home-assistant.io/integrations/duckdns/) or other add-on that enables secure HTTPS for your Home Assistant instance, you can get a secure WSS connection for OCPP.
To use a secure connection:
- Enable the option _Secure connection_
- Provide the pathways to your HA's SSL certificate and key files. These are typically located in the /config or /ssl folder, and typically named fullchain.pem and privkey.pem respectively.
- If you provide incorrect pathways, the integration will fail to setup with no clear indication of why.

If you do not use HTTPS for your Home Assistant instance:
- Disable the option _Secure connection_
- _Path to SSL certificate/key_ will be ignored.
<img width="576" src="https://user-images.githubusercontent.com/25015949/229125441-210554ee-8edf-4c3f-bb27-02c4634f2c6b.png">


### Measurands
- Most chargers only support a subset of all possible measurands. This depends most on the Feature profiles that are supported by the charger.
- The integration will autodetect the supported measurands when the charger connects. This can be disabled for chargers that do not support autodetection.

## Add the entities to your Dashboard
- On the OCPP integration, click on devices to navigate to your Charge Point device.

![image](https://user-images.githubusercontent.com/8673442/129495402-526a1863-9e9f-4a83-85de-d8add63a64ba.png)

- At the bottom of the Entities panel, click on 'Add to Lovelace' to add the entities to your dashboard.

![image](https://user-images.githubusercontent.com/8673442/129495159-611f4f86-aa90-4320-a69c-ce0870f6ee8c.png)

- An entity will have the value 'Unavailable' until the charger successfully connects.
- An entity will have the value 'Unknown' until its value has been read from the charger.

## Configure your Charger

- Configure your charger to use the OCPP websocket of your Central System (e.g. ws://homeassistant.local:9000). This is charger specific, so consult your manual.
- Some chargers require the protocol section 'ws://' to be removed, or require the url to end with a '/'.
- If you have configured _Secure connection_ in previous step, you should use 'wss://'
- Some chargers require the url to be specified as an IP address, i.e. '192.168.178.1:9000'
- You may need to reboot your charger before the changes become effective.
- If your charger has multiple connectors, wait until the charger device has populated its data (e.g., the Connectors sensor). Then reload the integration. A separate device will be created for each connector, and you’ll find all connector-specific entities there.

![image](https://user-images.githubusercontent.com/8673442/129495720-2ed9f0d6-b736-409a-8e14-fbd447dea078.png)

## Start Charging
- Use the charge control switch to start the charging process.

![image](https://user-images.githubusercontent.com/8673442/129495891-91f40bf9-f48e-4ced-b303-bf0fb77898f3.png)
