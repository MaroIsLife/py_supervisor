"""Log file (mandatory) + HTTP alerts (bonus)."""

import json
import logging
import threading
import time
import urllib.request


class EventLogger:
    def __init__(self):
        self.log = logging.getLogger("taskmaster")
        self.log.setLevel(logging.INFO)
        self.settings = None
        self.info, self.warning, self.error = self.log.info, self.log.warning, self.log.error

    def configure(self, settings):
        """(Re)open the log file."""
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            handler.close()
        handler = logging.FileHandler(settings.logfile)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        self.log.addHandler(handler)
        self.settings = settings

    def alert(self, event, process, message):
        """Bonus: send FATAL / unexpected exits to the http_alert URL.
        Done in a thread so a slow server never freezes the main loop."""
        if self.settings and self.settings.http_alert:
            alert = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
                     "process": process, "message": message}
            threading.Thread(target=self._send, args=(self.settings.http_alert, alert), daemon=True).start()

    def _send(self, url, alert):
        """HTTP POST of the alert as JSON (runs in its own thread)."""
        try:
            request = urllib.request.Request(url, data=json.dumps(alert).encode(),
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=5).close()
            self.info("alert: %s %s sent to %s", alert["event"], alert["process"], url)
        except Exception as e:      # receiver down: log it, never crash taskmaster
            self.error("alert: HTTP to %s failed: %s", url, e)
