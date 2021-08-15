![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

This is a Home Assistant integration for Electric Vehicle chargers that support the Open Charge Point Protocol.

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* [HACS](https://hacs.xyz/) compatible repository 

## Installation


1. ***Install HACS***
- If you have not yet installed HACS, go get it at https://hacs.xyz/ and walk through the installation and configuration.

2. **Install the OCPP repository**
- In Home Assistant, select HACS / Integrations / + Explore & add repositories. 

![image](https://user-images.githubusercontent.com/8673442/129494626-6e7a82b3-659f-4c39-a7be-43f70141cc7b.png)
- Search for 'OCPP' and install the repository.

3. ***Add the OCPP integration***
- In Home Assistant, select Configuration / Integrations / Add Integration. 

![image](https://user-images.githubusercontent.com/8673442/129494673-4718ba88-7872-435b-a331-66c8c34dddeb.png)
- Search for 'OCPP' and add the integration.

![image](https://user-images.githubusercontent.com/8673442/129494723-80e2e402-7564-4e86-b599-b87f32987ac0.png)

4. ***Configure the Central System***
- The default host address '0.0.0.0' will listen to all interfaces on your home assistant server.

![image](https://user-images.githubusercontent.com/8673442/129494762-08052152-f057-4563-93b5-5aae810dfbfc.png)
- Select which measurands you would like to become available as sensor entities.

![image](https://user-images.githubusercontent.com/8673442/129494804-cdff0dfb-a421-490c-af1e-e939f01455b4.png)

5. ***Configure your charger***
- Configure your charger to use the OCPP websocket (e.g. ws://homeassistant.local:9000). This is charger specific, so consult your manual. 

6. ***Add the entities to your dashboard***
- In Home Assistant, click on the the Charge Point device see the device info.
- Add the the entities to a lovelace dashboard using 'Add to Lovelace' at the bottom of the panel

![image](https://user-images.githubusercontent.com/8673442/129495159-611f4f86-aa90-4320-a69c-ce0870f6ee8c.png)

## Screenshot

![example](https://github.com/lbbrhzn/ocpp/raw/main/example.png "Example")

## Supported devices

All OCPP 1.6j compatible devices should be supported, but not every device offers the same level of functionality. So far, we've tried:

- [ABB Terra AC-W11-G5-R-0](https://new.abb.com/products/6AGC082156/tac-w11-g5-r-0)
- [Alfen - Eve Single Pro-line](https://alfen.com/en/ev-charge-points/alfen-product-range)
- [Alfen - Eve Single S-line](https://alfen.com/en/ev-charge-points/alfen-product-range)
- [EVLink Wallbox Plus](https://www.se.com/ww/en/product/EVH3S22P0CK/evlink-wallbox-plus---t2-attached-cable---3-phase---32a-22kw/)
- [Evnex E Series & X Series Charging Stations](https://www.evnex.com/)
- [Wallbox Pulsar](https://wallbox.com/en_uk/wallbox-pulsar)


## Devices with known issues
- [EVBox Elvi](https://evbox.com/en/products/home-chargers/elvi?language=en) appears to require a secure connection, which we do not support (yet).

## Debugging

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

## Support
**💡 Tip:** If you like this project consider buying me a cocktail 🍹:

<a href="https://www.buymeacoffee.com/lbbrhzn" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/default-black.png" alt="Buy Me A Coffee" width="150px">
</a>
