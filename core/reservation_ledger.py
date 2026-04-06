# core/reservation_ledger.py

"""
ReservationLedger — учёт зарезервированного капитала по pending-ордерам.

Решает проблему: при параллельных стратегиях на одном account_id
каждая видит полный free_money без учёта уже отправленных, но ещё не
исполненных заявок от других стратегий. Это приводит к тому,
что суммарный запрошенный капитал может превышать реально доступный.

Контракт:
- reserve(key, amount) — зарезервировать amount перед submit ордера
- release(key) — освободить резерв при fill/cancel/reject/timeout
- total_reserved(account_id) — суммарный зарезервированный капитал по счёту
- available(account_id, gross_free) — gross_free минус суммарные резервы
"""

import threading
import time
from typing import Dict, Optional

from loguru import logger

from core.money import to_storage_float
from core.runtime_metrics import runtime_metrics


class ReservationLedger:
    """Потокобезопасный учёт зарезервированного капитала на уровне счёта."""

    def __init__(self, stale_timeout_sec: float = 300.0, stale_cleanup_sec: float = 60.0):
        self._lock = threading.Lock()
        # key → {account_id, amount, ts}
        self._reservations: Dict[str, dict] = {}
        self._stale_timeout = stale_timeout_sec
        self._stale_cleanup_sec = stale_cleanup_sec

    def reserve(self, key: str, account_id: str, amount: float) -> None:
        with self._lock:
            self._reservations[key] = {
                "account_id": account_id,
                "amount": to_storage_float(amount),
                "ts": time.monotonic(),
                "created_at_epoch": time.time(),
                "order_id": "",
                "stale": False,
                "stale_reason": "",
                "stale_marked_at": 0.0,
            }
        logger.debug(
            f"[ReservationLedger] reserve key={key} account={account_id} amount={amount:.2f}"
        )

    def bind_order(self, key: str, order_id: str) -> bool:
        with self._lock:
            reservation = self._reservations.get(key)
            if not reservation:
                return False
            reservation["order_id"] = str(order_id or "")
            reservation["stale"] = False
            reservation["stale_reason"] = ""
            reservation["stale_marked_at"] = 0.0
            return True

    def mark_stale(self, key: str, reason: str) -> bool:
        with self._lock:
            reservation = self._reservations.get(key)
            if not reservation:
                return False
            if not reservation.get("stale"):
                reservation["stale_marked_at"] = time.monotonic()
            reservation["stale"] = True
            reservation["stale_reason"] = str(reason or "unknown")
            return True

    def snapshot(self) -> dict[str, dict]:
        removed = []
        with self._lock:
            now = time.monotonic()
            self._mark_stale_unsafe(now)
            removed = self._cleanup_stale_unsafe(now)
            snapshot = {key: dict(value) for key, value in self._reservations.items()}
        self._emit_cleanup_events(removed)
        return snapshot

    def release(self, key: str) -> None:
        with self._lock:
            removed = self._reservations.pop(key, None)
        if removed:
            logger.debug(
                f"[ReservationLedger] release key={key} "
                f"account={removed['account_id']} amount={removed['amount']:.2f}"
            )

    def total_reserved(self, account_id: str) -> float:
        removed = []
        with self._lock:
            now = time.monotonic()
            self._mark_stale_unsafe(now)
            removed = self._cleanup_stale_unsafe(now)
            reserved = sum(
                r["amount"]
                for r in self._reservations.values()
                if r["account_id"] == account_id
                and not r.get("stale")
            )
        self._emit_cleanup_events(removed)
        return reserved

    def available(self, account_id: str, gross_free: float) -> float:
        reserved = self.total_reserved(account_id)
        avail = gross_free - reserved
        return max(avail, 0.0)

    def _mark_stale_unsafe(self, now: float) -> None:
        """Помечает просроченные резервы stale. Вызывать только под self._lock."""
        for key, value in self._reservations.items():
            if value.get("stale"):
                continue
            if now - value["ts"] > self._stale_timeout:
                value["stale"] = True
                value["stale_reason"] = "timeout"
                value["stale_marked_at"] = now
                logger.warning(
                    f"[ReservationLedger] stale reservation marked key={key} "
                    f"account={value['account_id']} amount={value['amount']:.2f}"
                )

    def _cleanup_stale_unsafe(self, now: float) -> list[tuple[str, dict]]:
        """Удаляет stale-резервы после grace-периода. Вызывать только под self._lock."""
        removed: list[tuple[str, dict]] = []
        for key, value in list(self._reservations.items()):
            if not value.get("stale"):
                continue
            stale_marked_at = float(value.get("stale_marked_at", 0.0) or 0.0)
            if stale_marked_at <= 0:
                continue
            if now - stale_marked_at < self._stale_cleanup_sec:
                continue
            removed.append((key, dict(value)))
            del self._reservations[key]
        return removed

    def _emit_cleanup_events(self, removed: list[tuple[str, dict]]) -> None:
        for key, value in removed:
            logger.warning(
                f"[ReservationLedger] stale reservation cleanup key={key} "
                f"account={value['account_id']} amount={value['amount']:.2f} "
                f"reason={value.get('stale_reason', 'unknown')}"
            )
            runtime_metrics.emit_audit_event(
                "stale_reservation_cleanup",
                reservation_key=key,
                account_id=value.get("account_id", ""),
                amount=value.get("amount", 0.0),
                order_id=value.get("order_id", ""),
                stale_reason=value.get("stale_reason", "unknown"),
            )


# Глобальный singleton
reservation_ledger = ReservationLedger()
