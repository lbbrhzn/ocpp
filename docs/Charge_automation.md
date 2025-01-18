## Dynamically Adjusting EV Charge Current: A Smart Approach

Dynamically adjusting the charge current of an electric vehicle (EV) within a home automation system offers significant advantages:

* **Preventing Overload:** 
    * By monitoring real-time energy consumption, you can automatically reduce the EV's charging rate to prevent overloading the household's electrical circuits and potentially tripping the main fuse.

* **Optimizing Solar Power Usage:** 
    * When the solar panel production is avaiable in Home Assistent, you can prioritize charging the EV with excess solar energy.

* **Demand Response:** 
    * When you have dynamic energy pricing you charging rates based on time-of-use electricity pricing. B

This page provides several examples and hints to illustrate some of the many potential use cases."

## Adjusting the charge current

When the OCPP integration is added to your Home Assistent you get a slider to control the maximum charge current named: 
*number.<name_ocpp_charger>_maximum_current*

While using this entity in your automation might seem logical, it could potentially lead to permanent damage to your charger in the long run.
This entity controls the OCPP ChargePointMaxProfile, which configures the maximum power or current available for the entire charging station.
This setting is typically written to non-volatile storage (like EEPROM or flash memory) to persist across reboots.
Frequent writes to these types of memory can accelerate wear, potentially shortening the lifespan of your charger.

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

## solar current
The solar ystem usualy reports its production in Watt or kWatt. To convert this in the amps available for your EV-charger simply devide the Watt by the mains voltage (e.g 230V for the EU)
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

To avoid this, you can use the data provided by your smart meter sensors. By integrating smart meter data into your home automation system, you can dynamically adjust the EV charging rate based on real-time energy consumption. This ensures that the EV primarily charges using excess solar power while minimizing reliance on grid electricity during periods of high household demand.

