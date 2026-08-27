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

Nothing changes from previous releases. `devid` stays optional, and an action that omits it — or passes an id that no longer matches anything, after a rename or a re-pair — is still delivered to a charger of that central system, exactly as before.

## What happens with several

Actions are routed to the central system that owns the charger you named, so a charger on one system can no longer receive an action meant for another. In exchange, the id has to be good enough to identify one charger:

- **A `devid` that matches one charger** — delivered there. This is the normal case.
- **No `devid`, or one that matches nothing** — the action fails with *No charger found for device id ...*. There is no sensible default across several systems, so nothing is guessed.
- **A `devid` that matches a charger in more than one system** — the action fails as ambiguous. This happens when two chargers share a `cp_id`; pass the cpid of the one you mean instead.

If you have automations that rely on omitting `devid`, add it before configuring a second central system.

## Troubleshooting

**"No charger found for device id ..."** — the id matches no charger. Check it against the cpid shown in the charger's device page. A charger that is configured but has never connected is not routable either.

**"Charger id ... is ambiguous"** — two central systems each have a charger answering to that `cp_id`. Use the cpid, which is always unique. Alternatively give the chargers distinct OCPP identities in their own settings.

**"Charger is currently unavailable"** — the charger was found, but is not connected right now. This is about the connection, not the id.
