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
        "min_aspect_ratio": 0.3, "max_aspect_ratio": 4.0,
        "ir_saturation_threshold": 15.0, "ir_change_threshold_percent": 10.0, "ir_change_zscore": 1.0,
        "ir_min_pixel_std": 1.0, "ir_reference_max_samples": 5,
        "confirm_consecutive_scans": 1, "confirm_max_gap_minutes": 180.0,
        "notify_title": "Bin check", "notify_message": "The bin is still out front.",
        "log_level": "info",
    }
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(options))
    monkeypatch.setenv("BIN_SCANNER_OPTIONS", str(options_path))
    monkeypatch.setenv("BIN_SCANNER_IR_REFERENCE_DIR", str(tmp_path / "ir_reference"))
    monkeypatch.setenv("BIN_SCANNER_SCAN_STATE", str(tmp_path / "scan_state.json"))
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


def night_snapshot_bytes(patch=None) -> bytes:
    image = Image.new("L", (400, 300), 128).convert("RGB")
    if patch is not None:
        ImageDraw.Draw(image).rectangle(patch, fill=(220, 220, 220))
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
            "min_aspect_ratio": 0.3, "max_aspect_ratio": 4.0,
        },
    )
    assert preview_res.status_code == 200
    data = preview_res.get_json()
    assert data["detected"] is True
    assert data["red_pct"] > 10.0
    assert data["total_red_pct"] >= data["red_pct"]
    assert data["aspect_ratio"] is not None


def test_calibrate_preview_without_snapshot_yet(client):
    res = client.post("/calibrate/preview", json={})
    assert res.status_code == 400


def test_ir_reference_status_empty_by_default(client):
    res = client.get("/calibrate/ir-reference")
    assert res.status_code == 200
    data = res.get_json()
    assert data["exists"] is False
    assert data["captured_at"] is None


def test_capture_ir_reference_rejects_color_snapshot(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: sample_snapshot_bytes())
    client.get("/calibrate/snapshot")

    res = client.post("/calibrate/ir-reference")
    assert res.status_code == 400
    assert "infrared" in res.get_json()["error"].lower()


def test_capture_and_clear_ir_reference(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes())
    client.get("/calibrate/snapshot")

    capture_res = client.post("/calibrate/ir-reference")
    assert capture_res.status_code == 200
    assert capture_res.get_json()["count"] == 1

    status_res = client.get("/calibrate/ir-reference")
    status = status_res.get_json()
    assert status["exists"] is True
    assert status["count"] == 1
    assert status["captured_at"] is not None

    delete_res = client.delete("/calibrate/ir-reference")
    assert delete_res.status_code == 200
    assert client.get("/calibrate/ir-reference").get_json()["exists"] is False


def test_capture_ir_reference_rolls_off_oldest_beyond_max_samples(client, app_module, monkeypatch):
    app_module.OPTIONS["ir_reference_max_samples"] = 2
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes())

    for _ in range(3):
        client.get("/calibrate/snapshot")
        capture_res = client.post("/calibrate/ir-reference")
        assert capture_res.status_code == 200

    status = client.get("/calibrate/ir-reference").get_json()
    assert status["count"] == 2


def test_scan_infrared_without_reference_defaults_to_detected(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes())
    notified = {}
    monkeypatch.setattr(
        app_module, "call_notify_service", lambda service, title, message, timeout=10.0: notified.update(called=True)
    )

    res = client.post("/scan")
    assert res.status_code == 200
    data = res.get_json()
    assert data["detected"] is True
    assert data["confirmed"] is True
    assert data["mode"] == "infrared-no-reference"
    assert notified.get("called") is True


def test_scan_infrared_with_reference_uses_change_detection(client, app_module, monkeypatch):
    app_module.IR_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    (app_module.IR_REFERENCE_DIR / "0.jpg").write_bytes(night_snapshot_bytes())
    monkeypatch.setattr(
        app_module,
        "fetch_camera_snapshot",
        lambda camera_entity, timeout=15.0: night_snapshot_bytes(patch=(140, 100, 260, 260)),
    )
    monkeypatch.setattr(app_module, "call_notify_service", lambda *a, **k: None)

    res = client.post("/scan")
    assert res.status_code == 200
    data = res.get_json()
    assert data["mode"] == "infrared"
    assert data["detected"] is True
    assert data["confirmed"] is True


def test_scan_uses_multiple_reference_samples_for_adaptive_baseline(client, app_module, monkeypatch):
    app_module.IR_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (app_module.IR_REFERENCE_DIR / f"{i}.jpg").write_bytes(night_snapshot_bytes())
    monkeypatch.setattr(
        app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes()
    )
    monkeypatch.setattr(app_module, "call_notify_service", lambda *a, **k: None)

    res = client.post("/scan")
    assert res.status_code == 200
    data = res.get_json()
    assert data["mode"] == "infrared"
    assert data["detected"] is False


def test_scan_confirmation_debounce_requires_consecutive_detections(client, app_module, monkeypatch):
    app_module.OPTIONS["confirm_consecutive_scans"] = 2
    app_module.IR_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    (app_module.IR_REFERENCE_DIR / "0.jpg").write_bytes(night_snapshot_bytes())
    monkeypatch.setattr(
        app_module,
        "fetch_camera_snapshot",
        lambda camera_entity, timeout=15.0: night_snapshot_bytes(patch=(140, 100, 260, 260)),
    )
    notified = []
    monkeypatch.setattr(app_module, "call_notify_service", lambda *a, **k: notified.append(True))

    first = client.post("/scan").get_json()
    assert first["detected"] is True
    assert first["confirmed"] is False
    assert first["notified"] is False
    assert notified == []

    second = client.post("/scan").get_json()
    assert second["detected"] is True
    assert second["confirmed"] is True
    assert second["notified"] is True
    assert notified == [True]


def test_scan_confirmation_streak_resets_after_a_clear_scan(client, app_module, monkeypatch):
    app_module.OPTIONS["confirm_consecutive_scans"] = 2
    app_module.IR_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    (app_module.IR_REFERENCE_DIR / "0.jpg").write_bytes(night_snapshot_bytes())
    monkeypatch.setattr(app_module, "call_notify_service", lambda *a, **k: None)

    monkeypatch.setattr(
        app_module, "fetch_camera_snapshot",
        lambda camera_entity, timeout=15.0: night_snapshot_bytes(patch=(140, 100, 260, 260)),
    )
    first = client.post("/scan").get_json()
    assert first["confirmed"] is False

    monkeypatch.setattr(
        app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes()
    )
    clear_scan = client.post("/scan").get_json()
    assert clear_scan["detected"] is False

    monkeypatch.setattr(
        app_module, "fetch_camera_snapshot",
        lambda camera_entity, timeout=15.0: night_snapshot_bytes(patch=(140, 100, 260, 260)),
    )
    third = client.post("/scan").get_json()
    assert third["confirmed"] is False  # streak restarted, this is only the first detection again


def test_calibrate_preview_infrared_without_reference_errors(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_camera_snapshot", lambda camera_entity, timeout=15.0: night_snapshot_bytes())
    client.get("/calibrate/snapshot")

    res = client.post(
        "/calibrate/preview",
        json={"roi_x": 0.25, "roi_y": 0.55, "roi_width": 0.3, "roi_height": 0.35},
    )
    assert res.status_code == 400
    assert res.get_json()["mode"] == "infrared-no-reference"
