import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    options = {
        "camera_entity": "camera.front_doorbell",
        "notify_service": "mobile_app_test",
        "roi_x": 0.25, "roi_y": 0.55, "roi_width": 0.3, "roi_height": 0.35,
        "red_hue_min": 0, "red_hue_max": 12, "red_hue_min2": 245, "red_hue_max2": 255,
        "min_saturation": 90, "min_value": 60,
        "red_pixel_threshold_percent": 10.0,
        "notify_title": "Bin check", "notify_message": "The bin is still out front.",
        "log_level": "info",
    }
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(options))
    monkeypatch.setenv("BIN_SCANNER_OPTIONS", str(options_path))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    import importlib
    import main as main_module

    importlib.reload(main_module)  # pick up the tmp options file
    return main_module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def sample_snapshot_bytes() -> bytes:
    image = Image.new("RGB", (400, 300), (100, 120, 90))
    ImageDraw.Draw(image).rectangle((120, 180, 200, 260), fill=(200, 20, 20))
    buf = BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def test_calibrate_page_served(client):
    res = client.get("/calibrate")
    assert res.status_code == 200
    assert b"Bin Scanner" in res.data


def test_calibrate_options_reflects_config(client, app_module):
    res = client.get("/calibrate/options")
    assert res.status_code == 200
    assert res.get_json()["roi_x"] == app_module.OPTIONS["roi_x"]


def test_calibrate_snapshot_and_preview(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: sample_snapshot_bytes())

    snap_res = client.get("/calibrate/snapshot")
    assert snap_res.status_code == 200
    assert snap_res.mimetype == "image/jpeg"

    preview_res = client.post(
        "/calibrate/preview",
        json={
            "roi_x": 0.25, "roi_y": 0.55, "roi_width": 0.3, "roi_height": 0.35,
            "red_hue_min": 0, "red_hue_max": 12, "red_hue_min2": 245, "red_hue_max2": 255,
            "min_saturation": 90, "min_value": 60,
            "red_pixel_threshold_percent": 10.0,
        },
    )
    assert preview_res.status_code == 200
    data = preview_res.get_json()
    assert data["detected"] is True
    assert data["red_pct"] > 10.0


def test_calibrate_preview_without_snapshot_yet(client):
    res = client.post("/calibrate/preview", json={})
    assert res.status_code == 400
