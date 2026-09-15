Development
===========

It is recommended to use Visual Studio Code, and run home assistant in a devcontainer.
See [https://hacs.xyz/docs/developer/devcontainer](https://hacs.xyz/docs/developer/devcontainer)

Online development is supported through [GitHub Codespaces](https://github.com/features/codespaces)

## Reconnect lifecycle regression tests

Run the native Home Assistant tests for session ownership and bounded retirement:

```sh
pytest tests/test_reconnect_lifecycle.py --no-cov --timeout=30
```

These tests use the integration's real constructor and lifecycle methods. Controlled
transports exercise delayed finalizers, overlapping reconnects, repeated cancellation,
close failures, child-initiated stop, and cancellation-resistant cleanup. A separate
loopback WebSocket test uses real receive, Ping/Pong and Close without a charger.
One regression deliberately exercises the production 10-second retirement deadline.
That parameter is marked `slow`, but remains included in default runs and CI.
For quick local iteration only, add `-m 'not slow'`; run the unfiltered suite before
submitting changes. The loopback Ping/Pong is client-initiated, not a test of the
integration's delayed monitor ping loop.

A runner's cleanup owns its captured socket and children, not a replacement's mutable
fields. Retirement has one aggregate deadline for socket close and child settlement.
A timeout raises `TimeoutError`, retains and observes surviving tasks, and prevents
same-object replacement while those tasks remain alive. A rejected incoming socket
has its own cleanup budget, so a failed reconnect can consume two retirement budgets.
Cancellation cannot interrupt settlement; a cleanup error takes precedence over caller
cancellation. This bounds caller latency on a responsive event loop, not task lifetime
or complete process shutdown.

This is not a fence for separately scheduled `post_connect` or external service work.
It also does not change the central system's different-protocol object-rebuild fallback:
that fallback can construct a separate object after an old object's stop fails. Do not
infer cross-object isolation, command replay, or charger recovery guarantees from these
tests.
