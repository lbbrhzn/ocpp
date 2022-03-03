# Debugging

To enable debug logging for this integration and related libraries you
can control this in your Home Assistant `configuration.yaml`
file. Example:

```
logger:
  default: info
  logs:
    custom_components.ocpp: critical
    websocket: debug
```

After a restart detailed log entries will appear in `/config/home-assistant.log`.
