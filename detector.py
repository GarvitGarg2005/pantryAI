"""
detector.py - PantryAI Vision Pipeline
========================================

HOW RICE & DAL DETECTION WORKS
---------------------------------
Step 1 — YOLO detects a CONTAINER (bowl, cup, bottle, vase, etc.)
Step 2 — Only inside that container's bounding box, we run a colour
          analysis to decide what is IN it and how full it is.

  If the interior is mostly white/cream   → Rice container, level = white pixel %
  If the interior is mostly yellow/orange → Dal container,  level = yellow pixel %
  Otherwise                               → treat as the YOLO class (cup, bottle…)

This means:
  ✅  A rice bowl in frame → detected as Rice, level estimated
  ✅  A dal jar in frame   → detected as Dal,  level estimated
  ✅  An empty cup         → detected as Cup, level from bbox height
  ❌  Your t-shirt         → NOT inside any container bbox → ignored completely
  ❌  Background colour    → NOT inside any container bbox → ignored completely

COCO CLASSES USED AS "CONTAINERS"
----------------------------------
  bowl, cup, bottle, vase, wine glass, pot (potted plant→pot), clock
  — any enclosed/open container shape that could hold food/liquid.
  After YOLO finds these, the colour test decides the final label.

COLOUR THRESHOLDS (adjust if needed)
--------------------------------------
  RICE_HSV  : S < 55, V > 160  (white/cream grains)
  DAL_HSV   : H 10–35, S > 55  (yellow-orange lentils)
  MIN_CONTENT_RATIO : 0.12     (12 % of bbox must be content colour
                                 to classify as rice/dal vs plain container)
"""

import cv2
import numpy as np
from ultralytics import YOLO
import torch
import logging
from collections import defaultdict, deque

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Colour thresholds ────────────────────────────────────────────────────────
# Tune these if your rice/dal colour differs from the defaults.

RICE_LOW  = np.array([0,    0, 160])   # HSV lower — white/cream
RICE_HIGH = np.array([180, 55, 255])   # HSV upper

DAL_LOW   = np.array([10,  55,  70])   # HSV lower — yellow/orange lentils
DAL_HIGH  = np.array([35, 255, 255])   # HSV upper

# Minimum fraction of bbox interior that must match a colour to classify it
MIN_CONTENT_RATIO = 0.12   # 12 %

# COCO classes that can act as containers
CONTAINER_CLASSES = {
    'bowl', 'cup', 'bottle', 'vase', 'wine glass',
    'potted plant', 'clock', 'cell phone',   # fallback unusual containers
}

# Blinkit search terms
SEARCH_MAP = {
    'Water Bottle':   'water bottle 1L',
    'Cup':            'disposable cups',
    'Rice Container': 'basmati rice 5kg',
    'Dal Container':  'toor dal 1kg',
    'Biscuit Packet': 'parle g biscuits',
}

# Per-item reorder threshold %
THRESHOLD_MAP = {
    'Water Bottle':   30,
    'Cup':            30,
    'Rice Container': 30,
    'Dal Container':  30,
    'Biscuit Packet': 30,
}


# ===========================================================================
#  Colour-based content classifier
# ===========================================================================

def _color_ratio(crop: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    """Fraction of pixels in crop that fall within the HSV range [lo, hi]."""
    if crop is None or crop.size == 0:
        return 0.0
    hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lo, hi)
    return float(np.sum(mask > 0)) / (crop.shape[0] * crop.shape[1])


def classify_container(bbox, frame: np.ndarray, coco_class: str):
    """
    Given a YOLO-detected container bbox, look inside it and decide:
      - what it contains (rice / dal / generic)
      - the display name to use

    Returns (display_name, content_type)
    content_type: 'rice' | 'dal' | 'height' | 'binary'
    """
    x1 = max(int(bbox[0]), 0);  y1 = max(int(bbox[1]), 0)
    x2 = min(int(bbox[2]), frame.shape[1])
    y2 = min(int(bbox[3]), frame.shape[0])

    # Crop the interior — shrink by 10 % on each side to avoid container walls
    pad_x = max(int((x2 - x1) * 0.10), 2)
    pad_y = max(int((y2 - y1) * 0.10), 2)
    ix1, iy1 = x1 + pad_x, y1 + pad_y
    ix2, iy2 = x2 - pad_x, y2 - pad_y

    if ix2 <= ix1 or iy2 <= iy1:
        # Degenerate — just use COCO class
        return _coco_to_label(coco_class), 'height'

    interior = frame[iy1:iy2, ix1:ix2]

    rice_r = _color_ratio(interior, RICE_LOW,  RICE_HIGH)
    dal_r  = _color_ratio(interior, DAL_LOW,   DAL_HIGH)

    if rice_r >= MIN_CONTENT_RATIO and rice_r >= dal_r:
        return 'Rice Container', 'rice'
    if dal_r  >= MIN_CONTENT_RATIO and dal_r  > rice_r:
        return 'Dal Container',  'dal'

    # Neither — use the COCO class name as-is
    return _coco_to_label(coco_class), 'height'


def _coco_to_label(coco_class: str) -> str:
    mapping = {
        'bottle':       'Water Bottle',
        'cup':          'Cup',
        'bowl':         'Cup',          # empty bowl → cup-like
        'vase':         'Water Bottle', # tall vase → bottle-like
        'wine glass':   'Cup',
        'handbag':      'Biscuit Packet',
    }
    return mapping.get(coco_class, coco_class.replace('_', ' ').title())


# ===========================================================================
#  Fill-level estimator
# ===========================================================================

class FillLevelEstimator:
    """
    Three modes:
      'height'  — bbox height / rolling-max height × 100
      'rice'    — white-pixel ratio inside bbox / rolling-max × 100
      'dal'     — yellow-pixel ratio inside bbox / rolling-max × 100
      'binary'  — always 100 when present
    """
    WINDOW = 300

    def __init__(self):
        self._h   = defaultdict(lambda: deque(maxlen=self.WINDOW))
        self._cr  = defaultdict(lambda: deque(maxlen=self.WINDOW))

    def estimate(self, item_name: str, content_type: str,
                 bbox, frame: np.ndarray) -> int:
        if content_type == 'binary':
            return 100

        x1 = max(int(bbox[0]), 0);  y1 = max(int(bbox[1]), 0)
        x2 = min(int(bbox[2]), frame.shape[1])
        y2 = min(int(bbox[3]), frame.shape[0])

        if content_type == 'height':
            h = max(y2 - y1, 1)
            self._h[item_name].append(h)
            ref = max(self._h[item_name])
            return max(0, min(100, int(round(h / ref * 100))))

        # rice or dal — colour ratio inside interior crop
        pad_x = max(int((x2 - x1) * 0.10), 2)
        pad_y = max(int((y2 - y1) * 0.10), 2)
        ix1, iy1 = x1 + pad_x, y1 + pad_y
        ix2, iy2 = x2 - pad_x, y2 - pad_y

        if ix2 <= ix1 or iy2 <= iy1:
            return 100

        interior = frame[iy1:iy2, ix1:ix2]
        lo, hi   = (RICE_LOW, RICE_HIGH) if content_type == 'rice' \
                   else (DAL_LOW, DAL_HIGH)
        ratio    = _color_ratio(interior, lo, hi)

        self._cr[item_name].append(ratio)
        ref = max(self._cr[item_name])
        if ref < 1e-4:
            return 100   # calibrating
        return max(0, min(100, int(round(ratio / ref * 100))))


# ===========================================================================
#  PantryDetector
# ===========================================================================

class PantryDetector:

    ABSENCE_THRESHOLD   = 90    # frames before YOLO item is "gone"
    LOW_STOCK_THRESHOLD = 30    # default % threshold

    # COCO classes we want YOLO to find (containers + binary items)
    YOLO_WATCH = {
        # Container classes — will be reclassified by colour analysis
        'bowl', 'cup', 'bottle', 'vase', 'wine glass',
        # Binary / non-container items
        'handbag',   # → Biscuit Packet
    }

    # Non-container items — skip colour analysis, use fixed label + binary/height
    BINARY_ITEMS = {
        'handbag': ('Biscuit Packet', 'binary'),
    }

    def __init__(self):
        self.model  = YOLO('yolov8n.pt')
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model.to(self.device)

        self.frame_count = 0
        self.last_seen   = defaultdict(int)   # item_name → last frame seen
        self.fill_est    = FillLevelEstimator()
        self.inventory   = None   # injected by app.py

        # Tracked display names (built dynamically as items are first seen)
        self._known_items: dict = {}   # display_name → last status

        print(f"PantryDetector on {self.device}")
        print(f"  Watching YOLO classes : {sorted(self.YOLO_WATCH)}")
        print(f"  Container colour check: rice (white), dal (yellow-orange)")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def process_frame(self, frame: np.ndarray):
        """
        Run YOLO, classify containers by colour, estimate fill levels.
        Returns (annotated_frame, status_dict).

        status_dict = { display_name: { level_pct, search, threshold_pct,
                                        present, confidence, bbox } }
        """
        self.frame_count += 1
        results = self.model(frame, conf=0.45, device=self.device, verbose=False)

        current_detections: dict = {}  # display_name → detection data

        if results[0].boxes is not None:
            for box in results[0].boxes:
                cls_id    = int(box.cls[0])
                coco_name = self.model.names[cls_id]
                conf      = float(box.conf[0])

                if coco_name not in self.YOLO_WATCH or conf < 0.5:
                    continue

                coords = box.xyxy[0].cpu().numpy().astype(int)

                # ── Decide display name and fill mode ───────────────────────
                if coco_name in self.BINARY_ITEMS:
                    display_name, content_type = self.BINARY_ITEMS[coco_name]
                else:
                    # Colour-classify the interior of this container
                    display_name, content_type = classify_container(
                        coords, frame, coco_name
                    )

                # ── Estimate fill level ──────────────────────────────────────
                level = self.fill_est.estimate(
                    display_name, content_type, coords, frame
                )

                self.last_seen[display_name] = self.frame_count

                # Keep the highest-confidence detection if YOLO finds the same
                # display_name twice (e.g. two bowls both classified as Rice)
                existing = current_detections.get(display_name)
                if existing is None or conf > existing['confidence']:
                    current_detections[display_name] = {
                        'bbox':          coords,
                        'confidence':    conf,
                        'present':       True,
                        'level_pct':     level,
                        'content_type':  content_type,
                        'search':        SEARCH_MAP.get(display_name, display_name.lower()),
                        'threshold_pct': THRESHOLD_MAP.get(display_name, self.LOW_STOCK_THRESHOLD),
                    }

        # ── Update known-items registry ──────────────────────────────────────
        # Any item seen at least once is kept in _known_items so it always
        # appears in the dashboard list (as "absent" when not in frame).
        for name, data in current_detections.items():
            self._known_items[name] = data

        # ── Build full status dict (present + previously-seen absent items) ──
        status: dict = {}

        for display_name in list(self._known_items.keys()):
            thr = THRESHOLD_MAP.get(display_name, self.LOW_STOCK_THRESHOLD)

            if display_name in current_detections:
                status[display_name] = current_detections[display_name]
            else:
                fa        = self.frame_count - self.last_seen.get(display_name, 0)
                confirmed = fa >= self.ABSENCE_THRESHOLD
                status[display_name] = {
                    'bbox':          None,
                    'confidence':    0.0,
                    'present':       False,
                    'absent_frames': fa,
                    'level_pct':     0 if confirmed else self._known_items[display_name]['level_pct'],
                    'content_type':  self._known_items[display_name].get('content_type', 'height'),
                    'search':        SEARCH_MAP.get(display_name, display_name.lower()),
                    'threshold_pct': thr,
                }

        # ── Push to inventory ─────────────────────────────────────────────────
        self._update_inventory(status)

        # ── Annotate frame ────────────────────────────────────────────────────
        annotated = self._draw_detections(frame.copy(), status)
        return annotated, status

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _update_inventory(self, status: dict):
        if not self.inventory:
            return
        for name, info in status.items():
            self.inventory.update(
                name           = name,
                level_pct      = info['level_pct'],
                blinkit_search = info['search'],
                threshold_pct  = info['threshold_pct'],
            )

    def _draw_detections(self, frame: np.ndarray, status: dict) -> np.ndarray:
        cv2.putText(
            frame, f"PantryAI — Frame {self.frame_count}",
            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2
        )

        y_side = 60
        for item_name, data in status.items():
            level = data['level_pct']
            thr   = data['threshold_pct']
            color = (0, 255, 0) if level > thr else (0, 140, 255)

            if data.get('present') and data['bbox'] is not None:
                x1, y1, x2, y2 = data['bbox']
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

                ctype = data.get('content_type', '')
                tag   = f"[rice]" if ctype == 'rice' else \
                        f"[dal]"  if ctype == 'dal'  else ''
                label = f"{item_name}{tag}  {level}%  ({data['confidence']:.2f})"
                cv2.putText(frame, label, (x1, max(y1 - 10, 15)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                # Fill bar
                bar_w  = x2 - x1
                filled = int(bar_w * level / 100)
                cv2.rectangle(frame, (x1, y2 + 4), (x2, y2 + 12), (40, 40, 40), -1)
                cv2.rectangle(frame, (x1, y2 + 4), (x1 + filled, y2 + 12), color, -1)

                if level <= thr:
                    cv2.putText(frame, "LOW STOCK", (x1, y2 + 28),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 2)
            else:
                fa      = data.get('absent_frames', 0)
                conf    = fa >= self.ABSENCE_THRESHOLD
                col2    = (0, 0, 255) if conf else (0, 165, 255)
                cv2.putText(
                    frame,
                    f"{item_name}: {'ABSENT' if conf else 'not visible'} ({fa}f)",
                    (10, y_side), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col2, 1
                )
                y_side += 20

        return frame


# ===========================================================================
#  Quick standalone test
# ===========================================================================

if __name__ == '__main__':
    det = PantryDetector()
    cap = cv2.VideoCapture(0)
    print("Press 'q' to quit")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        out, status = det.process_frame(frame)
        if det.frame_count % 60 == 0:
            for n, d in status.items():
                flag = 'LOW' if d['level_pct'] <= d['threshold_pct'] else 'OK'
                print(f"  {n:22s} {d['level_pct']:3d}%  {flag}  "
                      f"type={d.get('content_type','?')}")
        cv2.imshow('PantryAI', out)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()
    cv2.destroyAllWindows()