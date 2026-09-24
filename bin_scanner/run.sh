#!/usr/bin/env bash
set -euo pipefail

# Options are read directly from /data/options.json by app/main.py (no bashio
# dependency, keeps the image on a plain python:alpine base).
exec python3 /app/main.py
