## Dynamically Adjusting EV Charge Current: A Smart Approach

Dynamically adjusting the charge current of an electric vehicle (EV) within a home automation system offers significant advantages:

* **Preventing Overload:**
    * By monitoring real-time energy consumption, you can automatically reduce the EV's charging rate to prevent overloading the household's electrical circuits and potentially tripping the main fuse.

* **Optimizing Solar Power Usage:**
    * When the solar panel production is available in Home Assistant, you can prioritize charging the EV with excess solar energy.

* **Demand Response:**
    * When you have dynamic energy pricing, you can adjust charging rates based on time-of-use electricity pricing.

This page provides several examples and hints to illustrate some of the many potential use cases."

## Adjusting the charge current

When the OCPP integration is added to your Home Assistant, you get a slider to control the maximum charge current named (or one per connector, if your charger has multiple connectors):
<mark>number.<name_ocpp_charger>_maximum_current</mark>

While using this entity in your automation might seem logical, it could potentially lead to permanent damage to your charger in the long run.
This entity controls the OCPP ChargePointMaxProfile, which configures the maximum power or current available for the entire charging station.
This setting is typically written to non-volatile storage (like EEPROM or flash memory) to persist across reboots.
Frequent writes to these types of memory can accelerate wear, potentially shortening the lifespan of your charger. Ten updates per day is no problem at all, 1 update per 10s could break your charger somewhere between 3 days and 3 years depending on the HW solution.

⚠️ **Warning**: Using the maximum current slider in automations can lead to permanent hardware damage due to frequent writes to non-volatile memory.

### TXprofile

Therefore, it is better to utilize a specific profile that is active exclusively during the current charging session. This approach allows you to adjust the charge current downwards while still respecting the upper limit defined by the ChargePointMaxProfile.

Essentially, the slider in your GUI maintains control over the absolute maximum current the charger can utilize.

To dynamically set the session-specific charge current within an automation, use the following action code snippet:

    - action: ocpp.set_charge_rate
      data:
        custom_profile: |
          {
            "transactionId": {{ states('sensor.<name_ocpp_charger>_transaction_id') | int }},
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Relative",
            "chargingSchedule": {
              "chargingRateUnit": "A",
              "chargingSchedulePeriod": [
                {"startPeriod": 0, "limit": {{ states('entity_charge_limit') | int }}}
              ]
            }
          }
        conn_id: 1


Where <mark>entity_charge_limit</mark> refers to your chosen entity (e.g., a number or sensor) that holds the desired current value.

NB! The action above only changes your current session limits. If you want to impact future sessions, run the same action, but replace <mark>"chargingProfilePurpose": "TxProfile"</mark> with <mark>"chargingProfilePurpose": "TxDefaultProfile"</mark>.

## solar current
The solar system usually reports its production in Watts or kW. To convert this to amps available for your EV charger, simply divide the watts by the mains voltage (e.g., 230V for the EU)

You can create a template sensor for this:

    - platform: template
      sensors:
        solaredge_amps:
        value_template: "{{ (states('sensor.solaredge_power') | round(0, 'floor')| float) / 230 }}"
        unit_of_measurement: 'A'
        device_class: current

This sensor contains the solar current in amps rounded down to the nearest whole number. It could be directly used in the action above.

## Smart-meter
The paragraph above suggests that nearly all the solar current is prioritized for the EV charger. However, this can lead to situations where other high-demand appliances, such as a washing machine or hot tub, still draw power from the grid even when solar energy is available.

To avoid this, you can use the data provided by your smart meter sensors. By integrating smart meter data into your home automation system, you can dynamically adjust the EV charging rate based on real-time energy consumption. This ensures that the EV primarily charges using excess solar power while minimizing reliance on grid electricity during periods of high household demand.you might have it as power.

For this it is important to know the current you deliver or receive from the grid. Depending on you smart meter sensors you might have this current available in a sensor or you might have this as power. The example below uses power to and from the grid and converts it to a current which is negative when receiving from the grid.
In this example the power is in kW and the current is calculated using at actual mains voltage.
This template sensor gives the right value:

        - platform: template
          sensors:
             grid_current_available:  # Calculate the current still available to use, can be negative
              value_template: >-
              {{((states('sensor.p1_meter_p1_returned') | float - states('sensor.p1_meter_p1_power') | float )*1000
              / (states('sensor.p1_meter_p1_voltage') | float )) }}
              unit_of_measurement: 'A'
              device_class: current

For solar charging a positive grid current avaiable means you can increase your EV charge number with this number, when negative you need to decrease. This means you need to know the actual charge current and modify this. To do this easily you can use a variable inside your automation to store the actual charge current.

   variables:
     actual_charge_current: "{{ (states('sensor.charge_current') | float) }}"
     new_amps: "{{ actual_charge_current + (states('sensor.grid_current_available') | float) }}

This could lead to a negative charge current, to avoid this create a new variable with a minimum value:

    charge_current: "{{ [new_amps, 0] | max }}"

The `max` filter ensures the charge current never goes below 0 amps, which would be invalid for the charging station.

## maximum charge
A simular solution could be used to check how much power is still available from the grid substracting all power used by other appliances in your house. This way you can charge you EV as fast as possible without overloading your main fuse.

:exclamation: By specificatiomn your main fuse can withand 1.2 its rate current for at least 1 hour

So a response time up to a minute is usually no problem if the overload is limited.
