#!/usr/bin/env python3
"""
ZT Runner API — Flask SSE server for classic Showroom solve/validate.

Endpoints:
  GET /health
  GET /config                    — list available modules
  GET /solve/<module>            — stream solve output via SSE
  GET /validate/<module>         — stream validation output via SSE
  GET /setup/<module>            — stream setup output via SSE
"""
from gevent import monkey
monkey.patch_all()

import logging
import os

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    from gunicorn.app.base import BaseApplication
    from stream_api import stream_app

    class StandaloneApplication(BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                if key in self.cfg.settings and value is not None:
                    self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    port = int(os.environ.get("PORT", 5000))
    workers = int(os.environ.get("GUNICORN_WORKERS", 2))
    options = {
        "bind": f"0.0.0.0:{port}",
        "workers": workers,
        "worker_class": "gevent",
        "timeout": 300,
        "keepalive": 5,
        "accesslog": "-",
        "errorlog": "-",
        "loglevel": os.environ.get("LOG_LEVEL", "info"),
    }
    logging.info("Starting ZT Runner on port %s with %s gevent workers", port, workers)
    StandaloneApplication(stream_app, options).run()
