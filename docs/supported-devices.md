Supported devices
=================

All OCPP 1.6j compatible devices should be supported, but not every device offers the same level of functionality. So far, we've tried:

## ABB Terra AC chargers

ABB Terra AC chargers with firmware version 1.8.21 and earlier fail to respond correctly when OCPP measurands are automatically detected by the OCPP integration. As of this writing, ABB has been notified, but no corresponding firmware fix is available.

### Issue Description

When automatic measurand detection is used in the OCPP integration with ABB Terra AC chargers:

1. The charger responds as if it supports all proposed measurands.
2. The integration then asks for all measurands to be reported.
3. When the integration tries to query which measurands are available after this configuration, the ABB Terra AC reboots.

As a result, the ABB charger becomes unusable with the OCPP integration, as the integration checks for available measurands on every charger boot, leading to a boot loop.

For more details and symptoms, see [Issue #1275](https://github.com/lbbrhzn/ocpp/issues/1275).

### Workaround

Fortunately, it is possible to configure the charger using manual configuration and to restore correct settings.

To use these chargers:

1. Disable "Automatic detection of OCPP Measurands".
   - Note: Automatic detection is enabled by default. Until configuration changes can be made online, you may need to remove the devices from this integration and add them again.
   - If "Automatic detection of OCPP Measurands" is disabled during configuration, you will be presented with a list of possible measurands.

2. When presented with the list of measurands, select only the following:
   - `Current.Import`
   - `Current.Offered`
   - `Energy.Active.Import.Register`
   - `Power.Active.Import`
   - `Voltage`

This list is based on the overview of OCPP 1.6 implementation for ABB Terra AC (firmware 1.6.6).

### [ABB Terra AC-W7-G5-R-0](https://new.abb.com/products/6AGC082156/tac-w7-g5-r-0)

### [ABB Terra AC-W11-G5-R-0](https://new.abb.com/products/6AGC082156/tac-w11-g5-r-0)

### [ABB Terra AC-W22-T-0](https://new.abb.com/products/6AGC081279/tac-w22-t-0)

### [ABB Terra TAC-W22-T-RD-MC-0](https://new.abb.com/products/6AGC081281/tac-w22-t-rd-mc-0)

## [Alfen - Eve Single Pro-line](https://alfen.com/en/ev-charge-points/alfen-product-range)

## [Alfen - Eve Single S-line](https://alfen.com/en/ev-charge-points/alfen-product-range)

## [Autel MaxiCharger](https://autelenergy.us/pages/residential)
The MaxiCharger works with the OCPP integration, but the community has run into a few quirks - see [Issue #1523](https://github.com/lbbrhzn/ocpp/issues/1523) for the full discussion.

Confirmed working on units reporting `MaxiChargerAC` as their model over `BootNotification` (vendor `Autel`, firmware `PFA0102|V0.00.00|V1.38.00||2.4.3.0`), speaking OCPP 1.6-J. Worth stating explicitly because Autel support has told at least one owner that their unit does not support OCPP and that only the AC Smart Elite does; that is not the case.

On a local network no TLS setup is needed - a plain `ws://<home-assistant-ip>:9000` server URL in the charger's OCPP settings is enough. The section below applies only if you are terminating TLS with a reverse proxy.

### Getting `wss://` (TLS) working behind a reverse proxy (e.g. Traefik)

If you're terminating TLS with a reverse proxy such as Traefik (e.g. using Let's Encrypt), the "certificate" field in the Autel's OCPP server setup is required. The charger will silently drop the `wss://` connection if you do not provide a certificate file.

You can either upload the self-generated certificate, or the root CA. An example for Let's Encrypt:

1. Download the ISRG Root X1 certificate from https://letsencrypt.org/certs/isrgrootx1.pem
2. Verify the chain validates against it before touching the charger:
   ```bash
   openssl s_client -connect your.ocpp.host:PORT \
     -servername your.ocpp.host \
     -CAfile isrg-root-x1.pem
   ```
   If you see `Verify return code: 0 (ok)`, uploading that certificate to the Autel will work.
3. Upload the certificate to the Autel's OCPP server configuration (the "certificate" field), along with your `wss://` server URL.

### Automation quirks

When writing automations against the MaxiCharger:

- Start/stop charging via `switch.<cpid>_charge_control`, not a remote-start service call.
- If a session is stopped via `switch.<cpid>_charge_control` while the car stays plugged in (e.g. leaving an off-peak window), the connector sits in `Finishing` rather than going back to `Available`. It won't accept a new session on its own when charging should resume (e.g. the next off-peak window) - press `button.<cpid>_reset` to start a new session without unplugging the car.
- Gate on `sensor.<cpid>_status_connector`, not `sensor.<cpid>_status`. The latter can read `Available` while a car is plugged in and simply not drawing current; the connector sensor reported `SuspendedEV` for the same charger at the same moment.

## [CTEK Chargestorm Connected 1, dual connectors](https://www.ctek.com/uk/ev-charging/chargestorm-connected-1)
See CTEK Chargestorm Connected 2 below for getting started instructions.

## [CTEK Chargestorm Connected 2](https://www.ctek.com/uk/ev-charging/chargestorm%C2%AE-connected-2)
[Jonas Karlsson](https://github.com/jonasbkarlsson) has written a [getting started guide](https://github.com/jonasbkarlsson/ocpp/wiki/CTEK-Chargestorm-Connected-2) for connecting CTEK Chargestorm Connected 2.

## [EN+ Caro Series Home Wallbox](https://www.en-plustech.com/product/caro-series-wallbox/)
This charger is often white-labelled by other vendors, including [cord](https://www.cord-ev.com/cord-one.html) and [EV Switch](https://www.evswitchstore.com.au/pages/ev-charger-range).

Note the charger's serial number - this is the number that you need to specify for the `Charge point identity` when you configure the OCPP integration in Home Assistant if the OCPP integration does not discover your charger, and also to request a firmware update for versions earlier than 1.0.25.130.

For firmware versions earlier than 1.0.25.130 the only way you can update firmware is by connecting to the evchargo OCPP server at `wss://ocpp16.evchargo.com:33033/` and emailing your serial number to `support@en-plus.com.cn` requesting that your firmware is updated.

You will probably want to update your firmware if it is earlier than 1.0.25.130 before configuring your charger to connect to your own OCPP server.

Firmware 1.0.25.130 has a firmware update option on the configuration interface (on IP address 192.168.4.1) which you can access by power-cycling the charger and connecting to its access point (see below).

If you have already installed the OCPP integration and have the default `charger` charge point installed, then you will need to re-configure this with the correct charge point identity (by removing and re-adding the OCPP integration) to change from the default `charger` charge point identity before configuring the charger.

Connect to the charger's access point (AP) by powering down the charger (i.e. switch off the charger's isolator or circuit breaker) and powering it back on a few seconds later.  The charger's access point becomes available for 15 minutes, and the SSID matches the charger's serial number (starting with SN).  Log in to the configuration interface on the IP address 192.168.4.1.

If doing this from a phone, you may need to set the phone to _Flight Mode_ first, and enable WiFi if required to enable the rest of the configuration to complete.

The username and password for the web interface are provided in the charger manual (case sensitive).

Configure the network mode to WiFi or Ethernet, and (in the field with an icon that looks like a router with three aerials) enter the address of your Home Assistant server including the port and protocol (e.g. `ws://myhomeassistant.tld:9000` or `wss://myhomeassistant.tld:9000` if you are using secure sockets).

The charger user interface will append the serial number when you leave the field - this is correct and expected.

Save, and the charger will reboot.

Reconnect to the charger's SSID, and log in again to 192.168.4.1 to confirm that the Network Status is online.  This confirms that the charger has an internet connection via Ethernet or WiFi and is connected to your OCPP server in Home Assistant.  Once enabled, the charger doesn't connect to the vendor server anymore and can be controlled only from Home Assistant or locally via Bluetooth.

Even though the device accepts all measurands, the key working ones are
   - `Current.Import`
   - `Current.Offered`
   - `Energy.Active.Import.Register`
   - `Voltage` - although this shows a constant voltage or zero unless a charging session is in progress.
   - `Transaction.ID`

You may wish to disable sensors that show `Unknown` after you've completed a charging session, as they will never provide data with the current firmware 1.0.25.130.

## [Etrel - Inch Pro](https://etrel.com/charging-solutions/inch-pro/)
To allow a custom OCPP server such as HA to set up a transaction ID, it is necessary to set under Users > Charging Authorization the
authorization type to either `Central system only` or `Charger whitelist and central system` otherwise the OCPP integration won't
match transactions and it won't report some meter values such as session time.

## [EVBox Elvi](https://evbox.com/en/ev-chargers/elvi)

## [Evnex E Series & X Series Charging Stations](https://www.evnex.com/)
(Ability to configure a custom OCPP server such as HA is being discontinued)

## [FoxESS A-series](https://www.fox-ess.com/) (tested: A022KP1, firmware 1.10/1.06)

Works well on OCPP 1.6J: remote start/stop, status, metering and the `Maximum Current` control all behave. The firmware's OCPP 2.0.1 support has several defects, all reproduced on the wire and confirmed by FoxESS support as protocol-level firmware defects, logged with their engineering team (no fix timeline yet). **Until fixed firmware ships, FoxESS's own guidance matches ours: run this charger on 1.6J.** On 2.0.1:

* `Maximum Current` does not work. The firmware validates 2.0.1 requests against its 1.6 schema and rejects every spec-valid `SetChargingProfile` with `PropertyConstraintViolation: csChargingProfiles validation failed` (a 1.6 field name), so the slider has no effect on the charger.
* Restarting a stopped charge can knock the charger offline. A `RequestStartTransaction` sent within a few minutes of a `RequestStopTransaction` is accepted and opens a transaction, but no power is ever offered to the vehicle (the charger reports `SuspendedEV`), and the charger then drops offline entirely. Automations that stop and later restart charging - tariff windows, state-of-charge limits - hit this path.
* Only session energy is reported by default (`TxUpdatedMeasurands = Energy.Active.Import.Register`), so Power/Current/Voltage sensors stay unavailable unless additional measurands are configured on the charger itself.
* `connectorStatus: Available` is reported after every transaction ends even with the cable still plugged in (on 1.6 the same firmware correctly reports `Finishing`/`Preparing`), so plug detection via the status sensor is unreliable.
* If the charger's app is left on protocol *auto* while the integration pins 2.0.1, the firmware sends 1.6-shaped messages over the negotiated `ocpp2.0.1` connection.

Reconnection (either protocol): the charger's OCPP client does not reliably redial after an established connection drops. After a Home Assistant restart or an integration reload, silent gaps of ~6 to 19+ minutes before the next connection attempt have been observed (occasionally it reconnects immediately), while a *refused* handshake - e.g. a protocol mismatch - is retried every ~10 seconds indefinitely. If the charger's entities stay unavailable after an HA restart, re-apply the protocol setting in the FoxESS app: that forces a reconnect within ~90 seconds.

## [Garo Entity Pro](https://www.garo.se/en/professional/products/e-mobility/wallbox/entity-pro/wallbox-entity-pro-22-sigi-o)

## [MaXpeedingrods Ev Charger](https://www.maxpeedingrods.com/category/ev-charger.html)

## [Mennekes Amtron Charge Control](https://www.mennekes.de/emobility/produkte/charge-control/)

## [Morek Smart AC Charger](https://ev.morek.eu/products-morek-quick-charge-11-22-kw/)
Successful connection requires firmware version **A0-MEV-V2.0.9** or newer.

The "Charger idle sampling interval" is not supported. Set this to **0** to avoid a "ClockAlignedDataInterval is read-only" warning.

## [Ocular LTE Plus v3](https://evse.com.au/) (tested: reports as BS-EV22, firmware 448.3251.0Q03197)
Works, with some firmware quirks worth knowing about. The same behaviour has been seen on the BS-EV07, so it likely applies to other chargers reporting the vendor `BS AC EV Charger`.

- In the charger's app, set MODE to ONLINE and enter the address without a scheme, for example `192.168.1.10:9000/central`. The examples shown in the app do not work.
- The charger sends its closing meter reading after StopTransaction rather than before. This is handled.
- After a remote stop the connector stays in `Finishing` until the cable is unplugged, and it rejects a remote start while it is in that state. To pause and resume a charge without unplugging, leave the session running and set the maximum current to 0 instead of stopping it.
- Firmware 448.3251.0Q03197 returns a corrupted ID tag, truncates configuration values, and occasionally sends a malformed connection request. These have been reported to the vendor.

## [Rolec EVO](https://www.rolecserv.com/ev-products/evo)
Tested single phase 7kW model (ROLEC5011) with firmware 1.2.7, appears to be working fine.

Need to configure the OCPP server using the **Rolec Connect** mobile app, and
set the current to the maximum (32A), otherwise the exposed `Maximum Current`
entity in HA will be capped.

You can still connect with the EVO app via Bluetooth after setting the OCPP server,
but certain features (eg. scheduling) may not work.

## [Schneider Electric EVlink ProAC](https://www.se.com/uk/en/product-range/23107242-evlink-pro-ac/)
"Supervision URL" setting must be set to the OCPP server address using eSetup mobile app or EcoStruxure Charging Configuration Tool (ECCT) PC software.

## [Schneider Electric EVlink Wallbox Plus](https://www.se.com/nl/en/product/EVH3S22P0CK/evlink-wallbox-plus-t2-attached-cable-3-phase-32a-22kw/)
Model is discontinued by the manufacturer.

## [Simpson & Partners](https://simpson-partners.com/home-ev-charger/)
All basic functions work properly

## [SyncEV Compact EVCP](https://sync.energy/support/instruction-manuals)
These are a discontinued (but cheap) 7kw 1PH smart charger, with an OCPP implementation that's seemingly quite close to standard, and tolerent.
Mine works well with the plugin, OCPP setup is done through the local AP-Wifi. The admin panel password is admin.
A few plugin tweaks to get full functionality...
   - Force SMART mode, to allow setting charge rates (use action ocpp.set_charge_rate) and retreiving meter values (use action ocpp.trigger_custom_message)
   - Manually specify the Measurands
      - Voltage
      - Temperature
      - Current.Offered
      - Current.Import
      - Power.Active.Import
      - Energy.Active.Import.Register
   - Create an automation triggering action: ocpp.trigger_custom_message with requested_message set to MeterValues on a schedule of your choice to retrieve the Measurands.
   - Optionally create an automation updating the hearbeat interval (you have to set a value different to the one in the chargepoint) when the chargepoint reboots.
   - I haven't tested using secure mode.
   - If you have problems with charging profiles, check your firmware version is 1.6.3 (the latest in Mar 2025)
   - Firmware updates can be done through the app, by reconnecting the charger to the original OCPP backend (wss://cpc.uk.charge.ampeco.tech:443/syncev/) and if it says you're on the latest, call them (+44 1952 983 940) to get it updated.

## [Teison Smart MINI Wallbox](https://www.teison.com/ac_smart_mini_ev_wallbox.html)
Use *My Teison* app to enable webSocket. In the socket URL field enter the address of your Home Assistant server including the port. In the socket port field enter *ocpp1.6* for insecure connection or *socpp1.6* for secure connection with certificates. Once enabled, charger doesn't connect to the vendor server anymore and can be controlled only from Home Assistant or locally via Bluetooth.

Even though the device accepts all measurands, the working ones are
   - `Current.Import`
   - `Energy.Active.Import.Register`
   - `Power.Active.Import`
   - `Temperature`
   - `Voltage`

If the devices loses connection to Home Assistant (due to Wi-Fi disconnection or update, for example) it doesn't seem to reconnect automatically. It is necessary to reboot the charger via Bluetooth for it to reconnect.

## [United Chargers Inc. - Grizzl-E](https://grizzl-e.com/about/)

Grizzl-E chargers with firmware 3.x.x work mostly without issue, such as the following:
* Grizzl-E Mini Connect 2024
* Grizzl-E Ultimate

Known issue: In firmware 03.09.0 amperage changes are accepted but not applied. This is due to the firmware accepting but not handling a value of `ChargePointMaxProfile` in `ChargerProfilePurpose`. United Chargers has stated that this will be addressed in firmware version 03.11.0.

Supported OCPP requests for the 3.x.x firmware are documented in a PDF on their site in under https://grizzl-e.com/connect-to-third-party-ocpp-backend/

Other Grizzl-E chargers on the 5.x.x firmware have some defects in OCPP implementation, which can be worked around. See [User Guide](https://github.com/lbbrhzn/ocpp/blob/main/docs/user-guide.md) section in Documentation for details.)

Grizzl-E has now locked OCPP support behind a $200 paywall https://grizzl-e.com/ocpp-access/ (support may [unlock](https://community.home-assistant.io/t/grizzl-e-charger-and-has-ocpp-integration/804698/23) access for devices purchased before Sept 2025).

## [V2C Trydan](https://v2charge.com/trydan)

## [Vestel EVC04-AC22SW](https://www.vestel-echarger.com/EVC04_HomeSmart22kW.html)

## [Wallbox Pulsar & Copper SB](https://wallbox.com/en_uk/wallbox-pulsar)
The Wallbox Pulsar and Copper SB have been verified.
In the OCPP-config, leave the password field empty.

## [ZJ Beny BCP-A2N-P](https://ultipower.com.au/products/zj-beny-home-charging-station-ac-7kw-32a-type-2-ocpp)
Note that there are different models with similar model names, some of which support OCPP and some with other features.

## Others
When a charger is not listed as a supported charger it simply means that it has not been reported to work. Whether it will work or not in practice really depends on whether it is compliant with the OCPP standard. Some vendors claim their device is compliant without bothering to do a compliance test, because that takes time and costs money!

When it is fully compliant, then it should work out of the box, since the ocpp integration is designed to work for fully compliant chargers. Any issues should be reported, and we will do out best to analyze them. In some cases modifications or workarounds may be needed. As long as these workarounds do not break compliance to the OCPP standard they can be added to this repository.
Otherwise, we urge you to request your vendor to update their firmware to make their device OCPP compliant.

You can always make your own fork of this repository to solve issues for a specific device that are not OCPP compliant. However, we will not integrate these type of changes into this repository, because that may prevent other chargers to work.
