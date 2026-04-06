from __future__ import annotations

import threading
import time
from collections import defaultdict


class RuntimeMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._latencies = {}
        self._audit_events = []

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_latency(self, name: str, value_ms: float) -> None:
        with self._lock:
            current = self._latencies.get(name, {})
            count = int(current.get("count", 0)) + 1
            total = float(current.get("total_ms", 0.0)) + float(value_ms)
            self._latencies[name] = {
                "last_ms": float(value_ms),
                "max_ms": max(float(current.get("max_ms", 0.0)), float(value_ms)),
                "count": count,
                "total_ms": total,
                "avg_ms": total / count if count else 0.0,
            }

    def emit_audit_event(self, event_type: str, **payload) -> None:
        event = {
            "event_type": str(event_type),
            "ts": time.time(),
            **payload,
        }
        with self._lock:
            self._audit_events.append(event)
            if len(self._audit_events) > 500:
                self._audit_events = self._audit_events[-500:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "latencies": dict(self._latencies),
                "audit_events": list(self._audit_events[-100:]),
            }


runtime_metrics = RuntimeMetrics()