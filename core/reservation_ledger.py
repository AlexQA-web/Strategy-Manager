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


class ReservationLedger:
    """Потокобезопасный учёт зарезервированного капитала на уровне счёта."""

    def __init__(self, stale_timeout_sec: float = 300.0):
        self._lock = threading.Lock()
        # key → {account_id, amount, ts}
        self._reservations: Dict[str, dict] = {}
        self._stale_timeout = stale_timeout_sec

    def reserve(self, key: str, account_id: str, amount: float) -> None:
        with self._lock:
            self._reservations[key] = {
                "account_id": account_id,
                "amount": amount,
                "ts": time.monotonic(),
            }
        logger.debug(
            f"[ReservationLedger] reserve key={key} account={account_id} amount={amount:.2f}"
        )

    def release(self, key: str) -> None:
        with self._lock:
            removed = self._reservations.pop(key, None)
        if removed:
            logger.debug(
                f"[ReservationLedger] release key={key} "
                f"account={removed['account_id']} amount={removed['amount']:.2f}"
            )

    def total_reserved(self, account_id: str) -> float:
        now = time.monotonic()
        with self._lock:
            self._evict_stale(now)
            return sum(
                r["amount"]
                for r in self._reservations.values()
                if r["account_id"] == account_id
            )

    def available(self, account_id: str, gross_free: float) -> float:
        reserved = self.total_reserved(account_id)
        avail = gross_free - reserved
        return max(avail, 0.0)

    def _evict_stale(self, now: float) -> None:
        """Удаляет резервы старше stale_timeout (защита от утечек)."""
        stale_keys = [
            k for k, v in self._reservations.items()
            if now - v["ts"] > self._stale_timeout
        ]
        for k in stale_keys:
            r = self._reservations.pop(k)
            logger.warning(
                f"[ReservationLedger] stale reservation evicted key={k} "
                f"account={r['account_id']} amount={r['amount']:.2f}"
            )


# Глобальный singleton
reservation_ledger = ReservationLedger()
