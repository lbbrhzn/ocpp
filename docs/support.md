# Support
=======

- [General](#general)
- [FAQ](#faq)
  - [too many notifications in home assistant](#too-many-notifications-in-home-assistant)

## General

If you need help, check out our [forum](https://github.com/lbbrhzn/ocpp/discussions) or submit an [issue](https://github.com/lbbrhzn/ocpp/issues).

## FAQ

### too many notifications in home assistant

The OCPP sends a notification when the charger is rebooted. This can be due to a bad network connection. The notifications can be managed with automations in home assistant. (see https://github.com/lbbrhzn/ocpp/discussions/938)

Example:

```
trigger:
  - platform: persistent_notification
    update_type:
      - added
    notification_id: ""
condition:
  - condition: template
    value_template: "{{ trigger.notification.title | lower == \"ocpp integration\" }}"
action:
  - delay:
      hours: 0
      minutes: 10
      seconds: 0
      milliseconds: 0
  - service: persistent_notification.dismiss
    data:
      notification_id: "{{ trigger.notification.notification_id }}"
mode: parallel
max: 10
```

### unstable behavior when setting the charger maximum current

If your charger is acting strange when you're changing the maximum current, or sending an ocpp.set_charge_rate action, it might help to clear the charging profiles from the charger.

Run the following action (from Developer Tools):

```
    - action: ocpp.clear_profile
      data:
        devid: charger
```

Where <mark>charger</mark> refers to your selected charger device identity.
