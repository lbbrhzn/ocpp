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
