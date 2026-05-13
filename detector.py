"""
detector.py - YOLOv8 Object Detection for PantryAI
-------------------------------------------------
Detects common household items using YOLOv8.
Estimates fill level via bounding-box height relative to a calibrated
"full" reference, then calls inventory.update() so the re-arm / restock
logic in InventoryManager works correctly.
"""

import cv2
import numpy as np
from ultralytics import YOLO
import torch
import logging
from collections import defaultdict, deque
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fill-level estimator
# ---------------------------------------------------------------------------

class FillLevelEstimator:
    """
    Estimates fill level (0-100%) from a bounding-box height.

    Strategy
    --------
    We keep a rolling maximum of the bounding-box height for each item class
    over the last CALIBRATION_WINDOW frames.  That rolling max is treated as
    the "full / reference" height.  The current height expressed as a
    percentage of that reference is the estimated fill level.

    For items whose quantity is binary (present = full, absent = empty) the
    estimator still works — it will return 100 % when detected and 0 % when
    absent, which is exactly what inventory.update() needs.
    """

    CALIBRATION_WINDOW = 300   # frames (~10 s at 30 fps)

    def __init__(self):
        # deque of (frame_id, box_height) per item
        self._height_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.CALIBRATION_WINDOW)
        )

    def update(self, item_name: str, frame_id: int, bbox) -> int:
        """
        Record a new detection and return current fill-level estimate (0-100).
        bbox must be (x1, y1, x2, y2).
        """
        h = int(bbox[3]) - int(bbox[1])
        self._height_history[item_name].append(h)
        return self._estimate(item_name, h)

    def absent(self, item_name: str) -> int:
        """Call when item is not detected this frame.  Returns 0."""
        return 0

    def _estimate(self, item_name: str, current_h: int) -> int:
        history = self._height_history[item_name]
        if not history:
            return 100
        ref_h = max(history)
        if ref_h == 0:
            return 100
        pct = int(round(current_h / ref_h * 100))
        return max(0, min(100, pct))


# ---------------------------------------------------------------------------
# PantryDetector
# ---------------------------------------------------------------------------

class PantryDetector:

    ABSENCE_THRESHOLD = 90          # frames before item is considered gone
    LOW_STOCK_THRESHOLD = 30        # % fill level that triggers reorder

    def __init__(self):
        self.model  = YOLO('yolov8n.pt')
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model.to(self.device)

        self.frame_count      = 0
        self.last_seen        = defaultdict(int)   # item_name → last frame id
        self.fill_estimator   = FillLevelEstimator()

        # Inventory manager injected by app.py
        self.inventory = None

        # Map COCO class name → display name
        self.monitored_items = {
            'bottle':  'Water Bottle',
            'cup':     'Cup',
            'banana':  'Banana',
            'apple':   'Apple',
            'handbag': 'Biscuit Packet',
        }

        # Blinkit search terms per display name
        self._search_map = {
            'Water Bottle':  'water bottle 1L',
            'Cup':           'disposable cups',
            'Banana':        'banana fresh',
            'Apple':         'apple red fresh',
            'Biscuit Packet':'parle g biscuits',
        }

        print(f"PantryDetector initialized on {self.device}")
        print(f"Monitoring {len(self.monitored_items)} item types")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def process_frame(self, frame):
        """
        Run YOLO on one frame.
        Returns (annotated_frame, status_dict).

        status_dict  →  { display_name: { level_pct, search, threshold_pct,
                                          present, confidence, bbox } }
        """
        self.frame_count += 1
        results = self.model(frame, conf=0.5, device=self.device, verbose=False)

        current_detections: dict[str, dict] = {}

        if results[0].boxes is not None:
            for box in results[0].boxes:
                cls_id   = int(box.cls[0])
                cls_name = self.model.names[cls_id]
                conf     = float(box.conf[0])

                if cls_name in self.monitored_items and conf > 0.6:
                    item_name = self.monitored_items[cls_name]
                    coords    = box.xyxy[0].cpu().numpy().astype(int)

                    # Fill-level estimate from bounding-box height
                    level_pct = self.fill_estimator.update(
                        item_name, self.frame_count, coords
                    )

                    self.last_seen[item_name] = self.frame_count
                    current_detections[item_name] = {
                        'bbox':          coords,
                        'confidence':    conf,
                        'present':       True,
                        'level_pct':     level_pct,
                        'search':        self._search_map.get(item_name, item_name.lower()),
                        'threshold_pct': self.LOW_STOCK_THRESHOLD,
                    }

        # Build full status dict (present + absent items)
        status: dict[str, dict] = {}
        for item_name in self.monitored_items.values():
            if item_name in current_detections:
                status[item_name] = current_detections[item_name]
            else:
                frames_absent = self.frame_count - self.last_seen.get(item_name, 0)
                is_absent     = frames_absent >= self.ABSENCE_THRESHOLD
                level_pct     = 0 if is_absent else 100   # treat "still in frame recently" as 100 until confirmed absent
                status[item_name] = {
                    'bbox':          None,
                    'confidence':    0.0,
                    'present':       False,
                    'absent_frames': frames_absent,
                    'level_pct':     level_pct,
                    'search':        self._search_map.get(item_name, item_name.lower()),
                    'threshold_pct': self.LOW_STOCK_THRESHOLD,
                }

        # Push to inventory (this is what triggers reorder emails)
        self._update_inventory(status)

        annotated = self._draw_detections(frame.copy(), status)
        return annotated, status

    def get_status_summary(self):
        summary = {}
        for item_name in self.monitored_items.values():
            frames_absent = self.frame_count - self.last_seen.get(item_name, 0)
            is_present    = frames_absent < self.ABSENCE_THRESHOLD
            summary[item_name] = {
                'present':       is_present,
                'absent_frames': frames_absent,
                'needs_reorder': frames_absent >= self.ABSENCE_THRESHOLD,
                'last_seen_frame': self.last_seen.get(item_name, 0),
            }
        return summary

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _update_inventory(self, status: dict):
        """
        Call inventory.update() for every monitored item.
        inventory.update() owns all the restock / re-arm / cooldown logic.
        """
        if not self.inventory:
            return

        for item_name, info in status.items():
            self.inventory.update(
                name          = item_name,
                level_pct     = info['level_pct'],
                blinkit_search= info['search'],
                threshold_pct = info['threshold_pct'],
            )

    def _draw_detections(self, frame, status: dict):
        cv2.putText(frame, f"PantryAI — Frame {self.frame_count}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        y = 60
        for item_name, data in status.items():
            if data['present'] and data['bbox'] is not None:
                x1, y1, x2, y2 = data['bbox']
                level = data['level_pct']
                color = (0, 255, 0) if level > self.LOW_STOCK_THRESHOLD else (0, 165, 255)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

                label = f"{item_name}  {level}%  ({data['confidence']:.2f})"
                cv2.putText(frame, label, (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                # Mini fill-level bar
                bar_w = x2 - x1
                filled = int(bar_w * level / 100)
                cv2.rectangle(frame, (x1, y2 + 4), (x2, y2 + 12), (50, 50, 50), -1)
                cv2.rectangle(frame, (x1, y2 + 4), (x1 + filled, y2 + 12), color, -1)
            else:
                frames_absent = data.get('absent_frames', 0)
                confirmed     = frames_absent >= self.ABSENCE_THRESHOLD
                status_str    = "ABSENT" if confirmed else "missing…"
                color         = (0, 0, 255) if confirmed else (0, 165, 255)
                text = f"{item_name}: {status_str} ({frames_absent}f)"
                cv2.putText(frame, text, (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
                y += 20

        return frame


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    detector = PantryDetector()
    cap = cv2.VideoCapture(0)
    print("Press 'q' to quit")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        processed, detections = detector.process_frame(frame)
        cv2.imshow('PantryAI Detection', processed)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()
    cv2.destroyAllWindows()