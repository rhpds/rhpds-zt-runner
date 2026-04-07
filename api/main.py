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
import logging
from stream_api import stream_app
import os

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info("Starting ZT Runner Flask SSE API on port %s", port)
    stream_app.run(host="0.0.0.0", port=port, threaded=True)
