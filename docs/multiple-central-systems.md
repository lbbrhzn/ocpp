Multiple central systems
========================

Most installations need only one central system: a single OCPP entry listening on one port, with every charger connecting to it. Some setups need more than one — chargers on separate networks, or a charger whose firmware insists on a particular port. Each extra central system is simply another instance of the integration, added the same way as the first, on its own port.

This page covers what changes once a second one exists.

## Identifying a charger in an action

Every OCPP action takes an optional `devid`:

```yaml
- action: ocpp.set_charge_rate
  data:
    devid: garage_charger
    limit_amps: 16
```

`devid` accepts either identifier of a charger:

| Identifier | Where it comes from | Unique? |
| --- | --- | --- |
| **cpid** — the charger id you chose in Home Assistant | you, during configuration | yes, across every central system |
| **cp_id** — the OCPP identity the charger reports | the charger's own settings | not guaranteed |

The **cpid is the one to use.** Home Assistant keeps it unique across every charger of every central system, because your entity ids are built from it. A `cp_id` is whatever the charger was shipped with, so two chargers on two different central systems can easily both call themselves `CP_1`.

## What happens with one central system

`devid` stays optional. An action that **omits** it is delivered to a charger of that central system, exactly as before, so existing automations written for a single charger keep working untouched.

What did change: a `devid` that **is** given but matches no charger now fails, where it used to fall through to a charger anyway. If an id in an automation went stale — after a rename or a re-pair — that automation was quietly acting on whichever charger came first, and now says so instead.

## What happens with several

Actions are routed to the central system that owns the charger you named, so a charger on one system can no longer receive an action meant for another. Omitting `devid` stops working here, because there is no longer one obvious target:

- **A `devid` that matches one charger** — delivered there. This is the normal case.
- **No `devid`** — the action fails. Nothing is guessed across several systems.
- **A `devid` that matches nothing** — the action fails with *No charger found for device id ...*. Check it against the cpid on the charger's device page.
- **A `devid` that matches a charger in more than one system** — the action fails as ambiguous. This happens when two chargers share a `cp_id`; pass the cpid of the one you mean instead.

If you have automations that rely on omitting `devid`, add it before configuring a second central system.

## Troubleshooting

**"No charger found for device id ..."** — the id matches no charger, or no id was given and more than one central system is configured. Check it against the cpid shown in the charger's device page. A charger that is configured but has never connected is not routable either.

**"Charger id ... is ambiguous"** — two central systems each have a charger answering to that `cp_id`. Use the cpid, which is always unique. Alternatively give the chargers distinct OCPP identities in their own settings.

**"Charger is currently unavailable"** — the charger was found, but is not connected right now. This is about the connection, not the id.
