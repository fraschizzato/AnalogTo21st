import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request

from detector import angle_to_temperature, crop_dial, detect

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
HISTORY_PATH = DATA_DIR / "history.jsonl"

DEFAULTS = {
    "monitor_enabled": True,
    "interval_seconds": 45,
    "led_brightness_pct": 45,
    "led_warmup_seconds": 10.0,
    "led_post_hold_seconds": 1.0,
    "snapshot_settle_seconds": 0.75,
    "min_brightness_retry_threshold": 55,
    "max_snapshot_attempts": 3,
    "crop_left": 0.21,
    "crop_right": 0.35,
    "crop_top": 0.11,
    "crop_bottom": 0.31,
    "pivot_x_ratio": 0.42,
    "pivot_y_ratio": 0.52,
    "detector_method": "auto",
    "arc_radius_ratio": 0.18,
    "arc_start_deg": -10,
    "arc_end_deg": 190,
    "arc_num_points": 100,
    "arc_dark_fraction": 0.35,
    "temp_offset": 0.0,
    "calibration_points": [
        {"angle": 225, "temp": 0},
        {"angle": 180, "temp": 20},
        {"angle": 135, "temp": 40},
        {"angle": 90, "temp": 60},
        {"angle": 45, "temp": 80},
        {"angle": 0, "temp": 100},
    ],
}

GEOMETRY_KEYS = {
    "crop_left",
    "crop_right",
    "crop_top",
    "crop_bottom",
    "pivot_x_ratio",
    "pivot_y_ratio",
    "arc_radius_ratio",
    "arc_start_deg",
    "arc_end_deg",
    "arc_num_points",
    "arc_dark_fraction",
}


def load_cfg():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_cfg(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class Service:
    def __init__(self):
        self.cfg = load_cfg()
        self.lock = threading.RLock()
        self.stop_evt = threading.Event()
        self.session = requests.Session()
        self.raw = None
        self.crop = None
        self.overlay = None
        self.state = {
            "temperature": None,
            "angle": None,
            "method": None,
            "confidence": 0,
            "status": "starting",
            "updated_at": None,
            "last_error": None,
        }
        self.history = deque(maxlen=500)

        if HISTORY_PATH.exists():
            try:
                for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-500:]:
                    self.history.append(json.loads(line))
            except (OSError, json.JSONDecodeError):
                pass

        if not self.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        threading.Thread(target=self.loop, daemon=True).start()

    @property
    def base(self):
        base_url = os.getenv("HA_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("HA_BASE_URL is not configured")
        return base_url

    @property
    def verify(self):
        return os.getenv("HA_VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on")

    def headers(self, json_payload=True):
        token = os.getenv("HA_TOKEN", "").strip()
        if not token:
            raise RuntimeError("HA_TOKEN is not configured")
        headers = {"Authorization": f"Bearer {token}"}
        if json_payload:
            headers["Content-Type"] = "application/json"
        return headers

    def led(self, on):
        entity = os.getenv(
            "HA_LIGHT_ENTITY", "light.espscaldabagno_scaldabagno_flash"
        )
        url = f"{self.base}/api/services/light/{'turn_on' if on else 'turn_off'}"
        payload = {"entity_id": entity}
        if on:
            payload["brightness_pct"] = float(self.cfg["led_brightness_pct"])

        response = self.session.post(
            url,
            headers=self.headers(),
            json=payload,
            timeout=10,
            verify=self.verify,
        )
        response.raise_for_status()

    def snapshot(self):
        entity = os.getenv(
            "HA_CAMERA_ENTITY", "camera.espscaldabagno_scaldabagno_camera"
        )
        response = self.session.get(
            f"{self.base}/api/camera_proxy/{entity}",
            headers=self.headers(False),
            timeout=30,
            verify=self.verify,
        )
        response.raise_for_status()

        encoded = np.frombuffer(response.content, np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("JPEG cannot be decoded")
        return image

    @staticmethod
    def frame_luma(image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return float(gray.mean())

    def publish(self, temperature, angle, meta):
        entity = os.getenv("HA_SENSOR_ENTITY", "sensor.scaldabagno_temperature")
        payload = {
            "state": "unknown" if temperature is None else f"{temperature:.1f}",
            "attributes": {
                "friendly_name": "Water heater temperature (CV)",
                "unit_of_measurement": "°C",
                "angle_deg": angle,
                "detector_method": meta.get("method"),
                "confidence": round(meta.get("confidence", 0), 3),
                "updated_at": datetime.now().isoformat(),
            },
        }
        response = self.session.post(
            f"{self.base}/api/states/{entity}",
            headers=self.headers(),
            json=payload,
            timeout=10,
            verify=self.verify,
        )
        response.raise_for_status()

    def process_cached(self, cfg=None, publish=False, record=False, status="preview"):
        """Recalculate crop and detection on the cached frame without taking a new photo."""
        with self.lock:
            if self.raw is None:
                self.state.update(
                    status="no_frame",
                    last_error="No acquired frame is available for reprocessing",
                )
                return False

            active_cfg = dict(self.cfg if cfg is None else cfg)
            try:
                self.crop = crop_dial(self.raw, active_cfg)
                result = detect(self.crop, active_cfg)
                self.overlay = result["debug"]
                temperature = angle_to_temperature(
                    result["angle"],
                    active_cfg["calibration_points"],
                    active_cfg.get("temp_offset", 0),
                )
                now = datetime.now().isoformat()

                if publish:
                    self.publish(temperature, result["angle"], result)

                if record:
                    entry = {
                        "ts": now,
                        "temperature": temperature,
                        "angle": result["angle"],
                        "method": result["method"],
                        "confidence": result["confidence"],
                    }
                    self.history.append(entry)
                    with HISTORY_PATH.open("a", encoding="utf-8") as history_file:
                        history_file.write(json.dumps(entry) + "\n")
                else:
                    entry = {
                        "temperature": temperature,
                        "angle": result["angle"],
                        "method": result["method"],
                        "confidence": result["confidence"],
                    }

                self.state.update(entry, status=status, updated_at=now, last_error=None)
                return True
            except Exception as exc:  # Keep the worker alive on acquisition/CV failures.
                self.state.update(
                    status="error",
                    last_error=str(exc),
                    updated_at=datetime.now().isoformat(),
                )
                return False

    def read_once(self):
        with self.lock:
            self.state["status"] = "capturing"
            self.state["last_error"] = None
            best_image = None
            best_luma = -1.0
            selected_luma = None
            attempts_done = 0
            priming_ok = False

            try:
                # Turn the light on early so LED output, camera AEC and the framebuffer
                # have time to settle before the frame used for measurement is requested.
                self.led(True)
                time.sleep(float(self.cfg.get("led_warmup_seconds", 10.0)))

                # Intentionally discard the first frame: on ESP32-CAM it can be stale/dark.
                try:
                    self.snapshot()
                    priming_ok = True
                except Exception:
                    pass

                settle = float(self.cfg.get("snapshot_settle_seconds", 0.75))
                if settle > 0:
                    time.sleep(settle)

                max_attempts = max(1, int(self.cfg.get("max_snapshot_attempts", 3)))
                min_luma = float(self.cfg.get("min_brightness_retry_threshold", 55))

                for attempt in range(max_attempts):
                    attempts_done = attempt + 1
                    image = self.snapshot()
                    luma = self.frame_luma(image)

                    if luma > best_luma:
                        best_luma = luma
                        best_image = image

                    if luma >= min_luma:
                        self.raw = image
                        selected_luma = luma
                        break

                    if attempt < max_attempts - 1 and settle > 0:
                        time.sleep(settle)
                else:
                    if best_image is None:
                        raise RuntimeError("No valid image was acquired")
                    self.raw = best_image
                    selected_luma = best_luma

                post_hold = float(self.cfg.get("led_post_hold_seconds", 1.0))
                if post_hold > 0:
                    time.sleep(post_hold)

                self.state["frame_luma"] = (
                    round(float(selected_luma), 1)
                    if selected_luma is not None
                    else None
                )
                self.state["snapshot_attempts"] = attempts_done
                self.state["priming_ok"] = priming_ok

            except Exception as exc:
                self.state.update(
                    status="error",
                    last_error=str(exc),
                    updated_at=datetime.now().isoformat(),
                )
                return False
            finally:
                try:
                    self.led(False)
                except Exception:
                    pass

        return self.process_cached(self.cfg, publish=True, record=True, status="ok")

    def loop(self):
        time.sleep(2)
        while not self.stop_evt.is_set():
            if self.cfg.get("monitor_enabled", True):
                self.read_once()
            self.stop_evt.wait(max(5, float(self.cfg.get("interval_seconds", 45))))


service = Service()
app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify(ok=True, status=service.state["status"])


@app.get("/api/state")
def state():
    return jsonify(
        state=service.state,
        settings=service.cfg,
        history=list(service.history)[-100:],
    )


@app.post("/api/read")
def manual_read():
    return jsonify(ok=service.read_once(), state=service.state)


@app.post("/api/reprocess")
def reprocess():
    payload = request.get_json(silent=True) or {}
    preview = dict(service.cfg)
    for key, value in payload.items():
        if key in GEOMETRY_KEYS:
            preview[key] = value

    ok = service.process_cached(
        preview,
        publish=False,
        record=False,
        status="preview",
    )
    return jsonify(ok=ok, state=service.state)


@app.post("/api/monitor/<action>")
def monitor(action):
    if action not in ("start", "stop"):
        return jsonify(ok=False), 400

    service.cfg["monitor_enabled"] = action == "start"
    save_cfg(service.cfg)
    return jsonify(ok=True, enabled=service.cfg["monitor_enabled"])


@app.post("/api/settings")
def settings():
    payload = request.get_json(force=True)
    with service.lock:
        for key, value in payload.items():
            if key in DEFAULTS:
                service.cfg[key] = value
        save_cfg(service.cfg)

    # Geometry changes are immediately visible on the cached frame.
    if any(key in GEOMETRY_KEYS for key in payload):
        service.process_cached(
            service.cfg,
            publish=False,
            record=False,
            status="preview",
        )

    return jsonify(ok=True, settings=service.cfg, state=service.state)


@app.get("/api/history")
def history():
    return jsonify(list(service.history))


def image_response(image):
    if image is None:
        return Response(status=204)

    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return Response(status=500)

    response = Response(encoded.tobytes(), mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/image/raw")
def raw():
    return image_response(service.raw)


@app.get("/image/crop")
def crop():
    return image_response(service.crop)


@app.get("/image/overlay")
def overlay():
    return image_response(service.overlay)
