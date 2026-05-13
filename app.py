"""
app.py  –  PantryAI Flask Server
----------------------------------
Two background threads:
  _capture_thread   — reads webcam at full speed, stamps each frame with an
                       incrementing ID so the inference thread can tell when
                       a genuinely NEW frame is available.
  _inference_thread — waits for a new frame ID, runs YOLO, encodes JPEG,
                       updates inventory, pushes SSE.

This prevents both the "camera freeze" (inference blocking capture) and the
"same frame reprocessed" issue (YOLO track() getting confused by duplicates).
"""

import threading
import time
import queue
import json
import cv2

from flask import Flask, Response, render_template, jsonify, stream_with_context

from detector import PantryDetector as ContainerDetector 
from inventory import InventoryManager
from reorder   import ReorderEngine

app = Flask(__name__)

# ── Singletons ────────────────────────────────────────────────────────────────
inventory   = InventoryManager()
detector    = ContainerDetector()
reorder_eng = ReorderEngine(inventory)

# ── Shared state between threads ──────────────────────────────────────────────
_raw_frame      = None          # latest BGR frame from camera
_raw_frame_id   = 0             # increments every time a new frame arrives
_latest_jpeg    = None          # latest annotated JPEG bytes for streaming
_raw_lock       = threading.Lock()
_jpeg_lock      = threading.Lock()

_sse_queues = []
_sse_lock   = threading.Lock()


# ── Thread 1: capture — never blocks, just keeps _raw_frame fresh ─────────────

def _capture_thread():
    global _raw_frame, _raw_frame_id

    # Try camera index 0, fall back to 1 if unavailable
    cap = None
    for idx in [0, 1, 2]:
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            print(f"[CAMERA] ✅ Opened camera index {idx}")
            break
        cap.release()
    else:
        print("[CAMERA] ❌ No camera found on indices 0-2")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS,          30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # keep buffer minimal to avoid stale frames

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.033)
            continue
        with _raw_lock:
            _raw_frame    = frame
            _raw_frame_id += 1

    cap.release()


# ── Thread 2: inference — processes each new frame exactly once ───────────────

def _inference_thread():
    global _latest_jpeg

    print("[INFERENCE] Waiting for first camera frame …")
    last_processed_id = -1

    while True:
        # Wait until a genuinely new frame is available
        with _raw_lock:
            current_id = _raw_frame_id
            frame      = _raw_frame

        if frame is None or current_id == last_processed_id:
            time.sleep(0.01)
            continue

        last_processed_id = current_id

        # Run YOLO detection + level estimation
        try:
            annotated, status = detector.process_frame(frame.copy())
        except Exception as exc:
            print(f"[INFERENCE] process_frame error: {exc}")
            time.sleep(0.1)
            continue

        # Update inventory for every container reported
        for name, info in status.items():
            try:
                level_pct      = int(info.get("level_pct", 0))
                blinkit_search = str(info.get("search", name.lower()))
                threshold_pct  = int(info.get("threshold_pct", 30))
                inventory.update(
                    name=name,
                    level_pct=level_pct,
                    blinkit_search=blinkit_search,
                    threshold_pct=threshold_pct,
                )
            except Exception as exc:
                print(f"[INFERENCE] inventory.update error for '{name}': {exc}")

        # Encode JPEG
        try:
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with _jpeg_lock:
                    _latest_jpeg = buf.tobytes()
        except Exception as exc:
            print(f"[INFERENCE] JPEG encode error: {exc}")

        # Push SSE update roughly once per second
        if detector.frame_count % 25 == 0:
            _push_sse({
                "type":      "update",
                "frame":     detector.frame_count,
                "inventory": inventory.get_snapshot(),
                "status":    {
                    k: {
                        "level_pct":   v.get("level_pct", 0),
                        "calibrating": v.get("calibrating", False),
                    }
                    for k, v in status.items()
                },
            })

        # Cap inference at ~10 fps to avoid overloading CPU/GPU
        time.sleep(0.1)


def _push_sse(data: dict):
    msg  = json.dumps(data)
    dead = []
    with _sse_lock:
        for q in _sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    def generate():
        while True:
            with _jpeg_lock:
                frame = _latest_jpeg
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
            time.sleep(0.04)   # ~25 fps stream cap

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/snapshot")
def api_snapshot():
    return jsonify(inventory.get_snapshot())


@app.route("/api/reorder/<path:item_name>", methods=["POST"])
def api_manual_reorder(item_name: str):
    from urllib.parse import unquote
    name = unquote(item_name)
    inventory._reorder_queue.append(name)
    return jsonify({"queued": name})


@app.route("/events")
def sse_events():
    q = queue.Queue(maxsize=30)
    with _sse_lock:
        _sse_queues.append(q)

    @stream_with_context
    def stream():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    return Response(stream(), mimetype="text/event-stream")


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=_capture_thread,   daemon=True, name="capture").start()
    threading.Thread(target=_inference_thread, daemon=True, name="inference").start()
    reorder_eng.start()

    print("\n🚀 PantryAI → http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000,
            debug=False, threaded=True, use_reloader=False)