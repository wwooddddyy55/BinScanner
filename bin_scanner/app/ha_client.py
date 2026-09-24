"""Thin wrapper around the Home Assistant REST API, reached through the
Supervisor proxy (available automatically to add-ons that set
`homeassistant_api: true` in config.yaml, via the injected SUPERVISOR_TOKEN).
"""
from __future__ import annotations

import os

import requests

SUPERVISOR_API = "http://supervisor/core/api"


class HomeAssistantError(RuntimeError):
    pass


def _headers() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise HomeAssistantError(
            "SUPERVISOR_TOKEN is not set - is homeassistant_api enabled in config.yaml?"
        )
    return {"Authorization": f"Bearer {token}"}


def fetch_camera_snapshot(camera_entity: str, timeout: float = 15.0) -> bytes:
    url = f"{SUPERVISOR_API}/camera_proxy/{camera_entity}"
    try:
        response = requests.get(url, headers=_headers(), timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HomeAssistantError(f"Failed to fetch snapshot from {camera_entity}: {exc}") from exc
    return response.content


def call_notify_service(service: str, title: str, message: str, timeout: float = 10.0) -> None:
    url = f"{SUPERVISOR_API}/services/notify/{service}"
    payload = {"title": title, "message": message}
    try:
        response = requests.post(url, headers=_headers(), json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HomeAssistantError(f"Failed to call notify.{service}: {exc}") from exc
