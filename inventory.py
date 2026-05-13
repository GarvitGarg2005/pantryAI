"""
inventory.py  –  PantryAI Inventory Manager
--------------------------------------------
Tracks fill levels reported by detector.py.

Re-arm / restock contract
--------------------------
  - Item goes LOW  (level <= threshold AND was ok AND armed)
        → queue reorder, disarm so duplicate emails can't fire
  - Item RESTOCKS  (level >  threshold AND was low)
        → set status "ok", re-arm, clear cooldown
        → the NEXT time it goes low a fresh email will fire
  - Cooldown (REORDER_COOLDOWN) prevents duplicate emails for the
    SAME low event, but a restock fully resets the cooldown so the
    next low always fires even if it is within the normal window.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Minimum gap between two emails for the SAME item WITHOUT a restock in between.
REORDER_COOLDOWN = 600   # seconds (10 min)


@dataclass
class ContainerState:
    name:              str
    level_pct:         int   = 100
    threshold_pct:     int   = 30
    # "ok" | "low" | "reorder_pending" | "ordered" | "skipped"
    status:            str   = "ok"
    blinkit_search:    str   = ""
    last_reorder_time: float = 0.0
    reorder_count:     int   = 0
    armed:             bool  = True   # False after trigger; True again after restock
    history:           List[int] = field(default_factory=list)


class InventoryManager:

    def __init__(self):
        self._containers:    Dict[str, ContainerState] = {}
        self._reorder_queue: List[str] = []

    # ── Called by detector.py (via app.py inference thread) ──────────────────

    def update(self, name: str, level_pct: int,
               blinkit_search: str, threshold_pct: int = 30):
        """
        Called every time the detector has a reading for an item.
        Handles restock detection, re-arming, and reorder queuing.
        Each container is tracked completely independently.
        """
        if name not in self._containers:
            self._containers[name] = ContainerState(
                name=name,
                blinkit_search=blinkit_search,
                threshold_pct=threshold_pct,
            )

        cs = self._containers[name]
        cs.level_pct      = level_pct
        cs.blinkit_search = blinkit_search
        cs.threshold_pct  = threshold_pct

        cs.history.append(level_pct)
        if len(cs.history) > 30:
            cs.history.pop(0)

        was_low = cs.status in ("low", "reorder_pending", "skipped", "ordered")

        if level_pct > threshold_pct:
            # ── Restocked ────────────────────────────────────────────────────
            if was_low:
                log.info(
                    f"[Inventory] ✅ '{name}' restocked ({level_pct}%) "
                    "— re-armed, cooldown cleared."
                )
            cs.status            = "ok"
            cs.armed             = True    # allow next low event to fire
            cs.last_reorder_time = 0.0     # clear cooldown so next drop fires immediately

        else:
            # ── Low stock ────────────────────────────────────────────────────
            if cs.status == "ok":
                cs.status = "low"
                log.warning(
                    f"[Inventory] 🚨 '{name}' below threshold "
                    f"({level_pct}% <= {threshold_pct}%)"
                )

            # Fire reorder only if armed AND cooldown has elapsed
            if cs.armed and self._cooldown_ok(name):
                cs.armed = False      # disarm until next restock
                self._enqueue(name)

    # ── Reorder queue interface ───────────────────────────────────────────────

    def pop_reorder_queue(self) -> Optional[str]:
        return self._reorder_queue.pop(0) if self._reorder_queue else None

    def mark_reorder_sent(self, name: str):
        """Call immediately after the confirmation email is dispatched."""
        cs = self._containers.get(name)
        if cs:
            cs.status            = "reorder_pending"
            cs.last_reorder_time = time.time()
            cs.reorder_count    += 1
            log.info(
                f"[Inventory] 📧 Email sent for '{name}' "
                f"(total #{cs.reorder_count})"
            )

    def mark_reorder_done(self, name: str):
        cs = self._containers.get(name)
        if cs:
            cs.status = "ordered"
            log.info(f"[Inventory] 🛒 Order placed for '{name}'")

    def mark_reorder_skipped(self, name: str):
        cs = self._containers.get(name)
        if cs:
            cs.status = "skipped"
            log.info(f"[Inventory] ⏭  Reorder skipped for '{name}'")

    # ── Dashboard helpers ─────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        snap = {}
        for name, cs in self._containers.items():
            snap[name] = {
                "level_pct":      cs.level_pct,
                "status":         cs.status,
                "reorder_count":  cs.reorder_count,
                "blinkit_search": cs.blinkit_search,
                "reorder_flag":   cs.status in ("low", "reorder_pending"),
            }
        return snap

    def get_container(self, name: str) -> Optional[ContainerState]:
        return self._containers.get(name)

    def all_names(self) -> List[str]:
        return list(self._containers.keys())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cooldown_ok(self, name: str) -> bool:
        cs = self._containers[name]
        return (time.time() - cs.last_reorder_time) >= REORDER_COOLDOWN

    def _enqueue(self, name: str):
        if name not in self._reorder_queue:
            self._reorder_queue.append(name)
            log.warning(
                f"[Inventory] 📬 Queued reorder for: '{name}'"
            )