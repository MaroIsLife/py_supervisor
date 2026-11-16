"""Log file (mandatory) + syslog / HTTP / e-mail alerts (bonus)."""

import json
import logging
import logging.handlers
import os
import smtplib
import threading
import time
import urllib.request
from email.message import EmailMessage


class EventLogger:
    def __init__(self):
        self.log = logging.getLogger("taskmaster")
        self.log.setLevel(logging.INFO)
        self.settings = None
        self.info, self.warning, self.error = self.log.info, self.log.warning, self.log.error

    def configure(self, settings):
        """(Re)open the log file, and syslog if enabled."""
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            handler.close()
        handler = logging.FileHandler(settings.logfile)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        self.log.addHandler(handler)
        if settings.syslog:   # bonus: /dev/log on Linux, /var/run/syslog on macOS
            address = "/dev/log" if os.path.exists("/dev/log") else "/var/run/syslog"
            handler = logging.handlers.SysLogHandler(address=address)
            handler.setFormatter(logging.Formatter("taskmaster: %(levelname)s %(message)s"))
            self.log.addHandler(handler)
        self.settings = settings

    def alert(self, event, process, message):
        """Bonus: send FATAL / unexpected exits by HTTP and/or e-mail.
        Done in a thread so a slow server never freezes the main loop."""
        if self.settings and (self.settings.http_alert or self.settings.email_alert):
            alert = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
                     "process": process, "message": message}
            threading.Thread(target=self._send, args=(self.settings, alert), daemon=True).start()

    def _send(self, settings, alert):
        if settings.http_alert:
            try:
                request = urllib.request.Request(settings.http_alert, data=json.dumps(alert).encode(),
                                                 headers={"Content-Type": "application/json"})
                urllib.request.urlopen(request, timeout=5).close()
                self.info("alert: %s %s sent to %s", alert["event"], alert["process"], settings.http_alert)
            except Exception as e:
                self.error("alert: HTTP to %s failed: %s", settings.http_alert, e)
        if settings.email_alert:
            mail = settings.email_alert
            try:
                msg = EmailMessage()
                msg["Subject"] = f"[taskmaster] {alert['event']}: {alert['process']}"
                msg["From"], msg["To"] = mail["from"], mail["to"]
                msg.set_content("\n".join(f"{k}: {v}" for k, v in alert.items()))
                with smtplib.SMTP(mail["host"], mail["port"], timeout=5) as smtp:
                    smtp.send_message(msg)
                self.info("alert: %s %s e-mailed to %s", alert["event"], alert["process"], mail["to"])
            except Exception as e:
                self.error("alert: e-mail via %s:%s failed: %s", mail["host"], mail["port"], e)
