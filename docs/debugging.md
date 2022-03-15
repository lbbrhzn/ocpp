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

![LOAD FULL HOME ASSISTANT LOG](https://user-images.githubusercontent.com/8673442/158488329-64a2e38a-24d2-40ff-8743-643ebb337408.png)

You can filter for OCPP related messages by typing 'ocpp' in the 'search logs' box at the top of the page.

![search logs](https://user-images.githubusercontent.com/8673442/158488440-a5ae8076-5f33-49dd-86cb-7521cc74d96a.png)

