Debugging
=========

To enable debug logging for this integration and related libraries you
need to update your Home Assistant `configuration.yaml` file:

```
logger:
  default: info
  logs:
    custom_components.ocpp: debug
    websocket: debug
```

After a restart detailed log entries will appear in `/config/home-assistant.log`.
The log file can be displayed in your webbrowser, by selecting:

Configuration / Settings / Logs / LOAD FULL HOME ASSISTANT LOG

![LOAD FULL HOME ASSISTANT LOG](https://user-images.githubusercontent.com/8673442/158487245-7d844c9e-fd67-46d9-a4b2-2af1ae522172.png)

You can filter for OCPP related messages by typing 'ocpp' in the 'search logs' box at the top of the page.

![search logs](https://user-images.githubusercontent.com/8673442/158487966-4e9dc3e3-6316-4cce-8215-56981e48a74e.png)
