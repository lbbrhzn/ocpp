User guide
==========

## Installing the OCPP Integration

Follow the steps listed in [README.md](https://github.com/lbbrhzn/ocpp/blob/main/README.md) to get started.  Below are some additional notes which may save you some time.

## Installing HACS (Home Assistant Community Store)

Installation of the HACS integration is a pre-requisite before you can install OCPP.  However, it's worth noting that HACS brings a lot of baggage along with it, which is annoying, but this is the price to pay for using a 3rd party repository installer such as HACS.  Having said that, once it's up and running, HACS stays out of the way unless you need to `Redownload` or `Remove` OCPP.

The 'baggage' referred to above, is every single repository available through HACS.  As you can imagine, this adds up to a huge amount of data being downloaded from the Github servers, and they get upset about it, displaying `Rate Limit` error messages.  You will see these error messages whenever you install HACS, but don't worry, the rate limit will reset after a few hours and HACS will be installed.  It's worth remembering never to remove HACS unless there is no other way to achieve whatever it is you're wanting to do.  Each time you reinstall, you'll be in for a wait of several hours so it's best avoided unless there is no other alternative.

## Configuring the Central System

![Central System Configuration](https://user-images.githubusercontent.com/8673442/129494762-08052152-f057-4563-93b5-5aae810dfbfc.png)

The `Central system identity` shown above with a default of `central` can be anything you like.  Whatever is entered in that field will be used as a device identifier in Home Assistant (HA), so it's probably best to avoid spaces and punctuation symbols, but otherwise, enter anything you like.

The `Charge point identity` shown above with a default of `charger` is a little different.  Whatever you enter in that field will determine the prefix of all Charger entities added to Home Assistant (HA).  My recommendation is that it's best left at the default of charger.  If you put anything else in that field, it will be used as the prefix for all Charger entities added to HA during installation, however, new entities subsequently added in later version releases sometimes revert to the default prefix, regardless of what was entered during installation.  So you end up with a mixture of different prefixes which can be avoided simply by leaving `Charge point identity` set to the default of `charger`.

![OCPP Measurands](https://user-images.githubusercontent.com/8673442/129494804-cdff0dfb-a421-490c-af1e-e939f01455b4.png)

Measurands (according to OCPP terminology) are actually metrics provided by the charger.  Each charger supports a subset of the available metrics and for each one supported, a sensor entity is available in HA.  Some of these sensor entities will give erroneous readings whilst others give no readings at all.  Sensor entities not supported by the charger will show as `Unknown` if you try to create a sensor entity for them.  Below is a table of the metrics I've found useful for the Wallbox Pulsar Plus.  Tables for other chargers will follow as contributions come in from owners of each supported charger.

OCPP integration can automatically detect supported measurands. However, some chargers have faulty firmware that causes the detection mechanism to fail. For such chargers, it is possible to disable automatic measurand detection and manually set the measurands to those supported by the charger. When set manually, selected measurands are not checked for compatibility with the charger and are requested from it. See below for OCPP compliance notes and charger-specific instructions in [supported devices](supported-devices.md).

## Useful Entities for Wallbox Pulsar Plus

### Metrics

* `Energy Active Import Register` or `Energy Session` (they give the same readings)
* `Power Active Import` (instantaneous charging power)
* `Current Offered` (maximum charging current available)
* `Voltage` (single phase models only, doesn't work on 3-phase)
* `Frequency` (single phase models only, doesn't work on 3-phase)
* `Time Session` (elapsed time from start of charging session)

### Diagnostics

* `Status Connector` (shows the current state of available/preparing/charging/finishing/suspended etc)
* `Stop Reason` (reason the charging session was stopped)

### Controls

* `Charge Control`
* `Availability` (must be set to ON before EV is plugged in)
* `Maximum Current` (sets maximum charging current available)
* `Reset`

## Useful Entities for ABB Terra AC

### Metrics

* `Current.Import` (instantaneous current flow to EV)
* `Energy.Active.Import.Register` (active energy imported from the grid)
* `Power.Active.Import` (instantaneous active power imported by EV)
* `Voltage` (instantaneous AC RMS supply voltage)


## Useful Entities for EVBox Elvi

### Metrics

* `Current Offered` (maximum charging current available)
* `Time Session` (elapsed time from start of charging session)
* `Temperature` (internal charger temperature)

### Diagnostics

* `Status Connector` (shows the current state of available/preparing/charging/finishing/suspended etc)
* `Stop Reason` (reason the charging session was stopped)

### Controls

* `Charge Control`
* `Availability` (OFF when something causes a problem or during a reboot etc)
* `Maximum Current` (sets maximum charging current available)
* `Reset`

## Useful Entities and Workarounds for United Chargers Grizzl-E

Comments below relate to Grizzl-E firmware version 5.633, tested Oct-Nov 2022.

### Metrics
The Grizzl-E updates these metrics every 30s during charging sessions:
* `Current Import` (current flowing into EV)
* `Power Active Import` (power flowing into EV)
* `Energy Active Import Register` (cumulative energy supplied to EV during charging session. Resets to zero at start of each session)
* `Time Session` (elapsed time from start of charging session)

### Diagnostics

* `Status Connector` (current charger state: available/preparing/charging/finishing/suspended etc)
* `Stop Reason` (reason the charging session was stopped)
* `Latency Pong` (elapsed time for charger's response to internet ping. Good for diagnosing connectivity issues. Usually less than 1000ms)
* `Version Firmware` (charger firmware version and build)

### Controls

* `Charge Control` (User switches to ON to start charging session, once charger is in Preparing state. Can be automated in HA - see this [comment in Issue #442](https://github.com/lbbrhzn/ocpp/issues/442#issuecomment-1295865797) for details)
* `Availability` (ON when charger is idle. OFF during active charging session, or when something causes a problem)
* `Maximum Current` (sets maximum charging current available. Reverts to value set by charger's internal DIP switch following reboots; tweak slider to reload)

## Useful Entities for Vestel EVC-04 Wallboxes

### Metrics

* `Energy Active Import Register` (cumulative energy supplied to EV during charging session. Resets to zero at start of each session)
* `Energy Active Import Interval` (in case you need the energy spent in total for the current charging session)
* `Power Active Import` (instantaneous charging power)
* `Current Import`
* `Time Session` (elapsed time from start of charging session)

### Diagnostics

* `Status Connector` (shows the current state of available/preparing/charging/finishing/suspended etc)
* `Stop Reason` (reason the charging session was stopped)

### Controls

* `Charge Control`
* `Availability` (must be set to ON before EV is plugged in)
* `Maximum Current` (sets maximum charging current available)
* `Reset`

## Useful Entities for Rolec EVO

### Metrics

* `Current Import`
* `Current Offered` (may be limited by the settings on the charger itself, check the EVO app)
* `Energy Session` (charge for present/last session - kWh)
* `Power Active Import` (active charging power - kW)
* `Temperature` (internal temperature - degrees C)
* `Time Session` (duration of active/last charging session)
* `Voltage` (seems to report a little higher than expected)

There are several other metrics too, I'm not sure what they mean, and also `Export` variants of some of the `Import` entities, but they seem to always be zero for me.

### Diagnostics

* `Status Connector` (Available, Preparing, Charging, etc)

There are many other diagnostic entities about the features, ids, model, firmware etc, not sure if they'd be much practical use.

### Controls

* `Availability` (turning off switches the halo from flashing blue to constant red)
* `Charge Control`
* `Maximum Current` (if `Current Offered` doesn't reach this when charging, raise the current to the max in the EVO app itself, connect via Bluetooth)
* `Reset` (reboot the charger)
* `Unlock` (I think this will unlock the charging cable, if permanent lock is enabled from the app)

## OCPP Compatibility Issues

### ABB Terra AC

ABB Terra AC firmware 1.8.21 and earlier versions fail to respond correctly when OCPP measurands are automatically detected by the OCPP integration. As of this writing, ABB has been notified, but no corresponding firmware fix is available. As a result, users must configure measurands manually. See the suggested ABB Terra AC configuration in [supported devices](supported-devices.md).

### Grizzl-E

Grizzl-E firmware 5.x has a few OCPP-compliance defects, including responding to certain OCPP server messages with invalid JSON. Firmware 3.x.x on chargers such as the Mini Connect and Ultimate does not seem to have these issues. Symptoms of this problem include repeated reboots of the charger. By editing the OCPP server source code, one can avoid these problematic messages and obtain useful charger behaviour. ChargeLabs (the company working on the Grizzl-E firmware) expects to release version 6 of the firmware in early 2023, which may fix these problems.

The workaround consists of:
- checking the *Skip OCPP schema validation* checkbox during OCPP server configuration
- commenting-out several lines in `/config/custom_components/ocpp/api.py` and adding a few default values to the OCPP server source code. Details are in this [comment in Issue #442](https://github.com/lbbrhzn/ocpp/issues/442#issuecomment-1237651231)
