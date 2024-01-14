Supported devices
=================

All OCPP 1.6j compatible devices should be supported, but not every device offers the same level of functionality. So far, we've tried:

## [ABB Terra AC-W7-G5-R-0](https://new.abb.com/products/6AGC082156/tac-w7-g5-r-0)

## [ABB Terra AC-W11-G5-R-0](https://new.abb.com/products/6AGC082156/tac-w11-g5-r-0)

## [ABB Terra AC-W22-T-0](https://new.abb.com/products/6AGC081279/tac-w22-t-0)

## [Alfen - Eve Single Pro-line](https://alfen.com/en/ev-charge-points/alfen-product-range)

## [Alfen - Eve Single S-line](https://alfen.com/en/ev-charge-points/alfen-product-range)

## [CTEK Chargestorm Connected 2](https://www.ctek.com/uk/ev-charging/chargestorm%C2%AE-connected-2)
[Jonas Karlsson](https://github.com/jonasbkarlsson) has written a [getting started guide](https://github.com/jonasbkarlsson/ocpp/wiki/CTEK-Chargestorm-Connected-2) for connecting CTEK Chargestorm Connected 2.

## [Etrel - Inch Pro](https://etrel.com/charging-solutions/inch-pro/)
To allow a custom OCPP server such as HA to set up a transaction ID, it is necessary to set under Users > Charging Authorization the
authorization type to either `Central system only` or `Charger whitelist and central system` otherwise the OCPP integration won't
match transactions and it won't report some meter values such as session time.

## [EVBox Elvi](https://evbox.com/en/ev-chargers/elvi)

## [EVLink Wallbox Plus](https://www.se.com/ww/en/product/EVH3S22P0CK/evlink-wallbox-plus---t2-attached-cable---3-phase---32a-22kw/)

## [Evnex E Series & X Series Charging Stations](https://www.evnex.com/) 
(Ability to configure a custom OCPP server such as HA is being discontinued)

## [Garo Entity Pro](https://www.garo.se/en/professional/products/e-mobility/wallbox/entity-pro/wallbox-entity-pro-22-sigi-o)

## [MaXpeedingrods Ev Charger](https://www.maxpeedingrods.com/category/ev-charger.html)

## [United Chargers Inc. - Grizzl-E](https://grizzl-e.com/about/)  
(has some defects in OCPP implementation, which can be worked around. See [User Guide](https://github.com/lbbrhzn/ocpp/blob/main/docs/user-guide.md) section in Documentation for details.)

## [V2C Trydan](https://v2charge.com/trydan)

## [Vestel EVC04-AC22SW](https://www.vestel-echarger.com/EVC04_HomeSmart22kW.html)

## [Wallbox Pulsar](https://wallbox.com/en_uk/wallbox-pulsar)
The Wallbox Pulsar Max has been verified. 
In the OCPP-config, leave the password field empty.

## Others
When a charger is not listed as a supported charger it simply means that it has not been reported to work. Whether it will work or not in practice really depends on whether it is compliant with the OCPP standard. Some vendors claim their device is compliant without bothering to do a compliance test, because that takes time and costs money!

When it is fully compliant, then it should work out of the box, since the ocpp integration is designed to work for fully compliant chargers. Any issues should be reported, and we will do out best to analyze them. In some cases modifications or workarounds may be needed. As long as these workarounds do not break compliance to the OCPP standard they can be added to this repository.
Otherwise, we urge you to request your vendor to update their firmware to make their device OCPP compliant.

You can always make your own fork of this repository to solve issues for a specific device that are not OCPP compliant. However, we will not integrate these type of changes into this repository, because that may prevent other chargers to work.
