[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![codecov](https://codecov.io/gh/lbbrhzn/ocpp/branch/main/graph/badge.svg?token=3FRJIF5KRW)](https://codecov.io/gh/lbbrhzn/ocpp)
[![Documentation Status](https://readthedocs.org/projects/home-assistant-ocpp/badge/?version=latest)](https://home-assistant-ocpp.readthedocs.io/en/latest/?badge=latest)
[![hacs_downloads](https://img.shields.io/github/downloads/djiesr/ocpp/latest/total)](https://github.com/djiesr/ocpp/releases/latest)

![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

This is a temporary version awaiting the necessary fixes from United Chargers for the compatibility of the Grizzl-E Smart charger with the OCPP standards used in the original version created by lbbrhzn.

From [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp), patched for **Grizzl-E Smart** charger.

Tested with the firmware: **GWM-07.013-03_GCW-10.18-05.7**

- No special instruction

Tested with the firmware: **GWM-07.020-03_GCW-10.18-05.7**, **GWM-07.023-03_GCW-10.19-05.7** and **GWM-07.025-03_GCW-10.19-05.7**

- **Before install the integration**, copy de "ocpp" folder of this [OPCC package](https://github.com/mobilityhouse/ocpp) in your "HA Config"/deps/lib/python3.11/site-packages/
- Than, you will modify BootNotification.json in folder "HA Config"/deps/lib/python3.11/site-packages/ocpp/v16/schemas/ at line 12 replace "maxLength": 20 by "maxLength": 30
- After you can do the instalation the integration.
- When you will add the intégration, check the box of "ignore the validation of the OCPP scheme" like:
<img width="607" alt="image" src="https://github.com/djiesr/ocpp/assets/31359825/cacdfdbf-46e3-47e5-8ca2-9a8294474124">

(In french sorry)
- And, finish the instalation.

All other information is in the documentation you can found here [home-assistant-ocpp.readthedocs.io](https://home-assistant-ocpp.readthedocs.io)

* based on the [Python OCPP Package](https://github.com/mobilityhouse/ocpp).
* HACS compliant repository 



**💡 Tip:** If you like this project consider buying a coffee or a cocktail **to lbbrhzn**:

<a href="https://www.buymeacoffee.com/lbbrhzn" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/default-black.png" alt="Buy A Coffee To lbbrhzn" width="150px">
</a>








