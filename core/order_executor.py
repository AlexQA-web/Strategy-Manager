# core/order_executor.py

"""
Исполнитель ордеров.

Инкапсулирует логику размещения, мониторинга и завершения ордеров:
- рыночные ордера (открытие/закрытие)
- лимитные ордера по фиксированной цене
- chase-ордера (лимитка по стакану)
- мониторинг исполнения
- динамический расчёт лота
"""

import math
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Callable

from loguru import logger

from core.base_connector import Action, Side, OrderMode, OrderOutcome, MarketDataEnvelope

from config.settings import TRADING_END_TIME_MIN
from core.chase_order import ChaseOrder
from core.instrument_normalizer import (
    build_constraints,
    normalize_notional,
    normalize_price,
    normalize_quantity,
)
from core.order_lifecycle import OrderLifecycle, OrderState, pending_order_registry
from core.reservation_ledger import reservation_ledger
from core.runtime_metrics import runtime_metrics
from core.storage import get_setting
from core.telegram_bot import notifier, EventCode


def _get_account_gross_exposure(account_id: str) -> float:
    """Рассчитывает суммарную gross-экспозицию по всем позициям счёта.

    Включает pending-резервации из reservation_ledger.
    """
    from core.position_manager import position_manager

    total = 0.0
    for pos in position_manager.get_positions(account_id):
        qty = abs(float(pos.get("quantity", 0)))
        price = float(pos.get("current_price", 0) or pos.get("avg_price", 0))
        lot_size = int(pos.get("lot_size", 1) or 1)
        if qty > 0 and price > 0:
            total += qty * price * lot_size

    total += reservation_ledger.total_reserved(account_id)
    return total


def _get_account_positions_count(account_id: str) -> int:
    """Подсчитывает количество открытых позиций на счёте."""
    from core.position_manager import position_manager

    count = 0
    for pos in position_manager.get_positions(account_id):
        qty = float(pos.get("quantity", 0))
        if qty != 0:
            count += 1
    return count


class OrderExecutor:
    """Исполнитель ордеров для одной стратегии/тикета.

    Зависимости:
        connector: коннектор к бирже
        position_tracker: трекер позиции
        trade_recorder: регистратор сделок
        risk_guard: circuit breaker
    """

    def __init__(
        self,
        strategy_id: str,
        connector,
        position_tracker,
        trade_recorder,
        risk_guard,
        account_id: str,
        ticker: str,
        board: str,
        agent_name: str,
        order_mode: OrderMode = "market",
        lot_sizing: dict = None,
        get_last_price: Callable = None,
        get_last_price_envelope: Callable = None,
        get_point_cost: Callable = None,
        get_lot_size: Callable = None,
        is_futures: Callable = None,
        calculate_commission: Callable = None,
        on_reconcile: Callable = None,
        on_circuit_break: Callable = None,
        on_manual_intervention: Callable[[str], None] = None,
    ):
        self._strategy_id = strategy_id
        self._connector = connector
        self._position_tracker = position_tracker
        self._trade_recorder = trade_recorder
        self._risk_guard = risk_guard
        self._account_id = account_id
        self._ticker = ticker
        self._board = board
        self._agent_name = agent_name
        self._order_mode = order_mode
        self._lot_sizing = lot_sizing or {}
        self._get_last_price = get_last_price or (lambda: 0.0)
        self._get_last_price_envelope = get_last_price_envelope or (lambda: None)
        self._get_point_cost = get_point_cost or (lambda: 1.0)
        self._get_lot_size = get_lot_size or (lambda: 1)
        self._is_futures = is_futures or (lambda: False)
        self._calculate_commission = calculate_commission
        self._on_reconcile = on_reconcile
        self._on_circuit_break = on_circuit_break
        self._on_manual_intervention = on_manual_intervention

        self._chase_lock = threading.Lock()
        self._active_chase_orders: list = []
        self._running = True
        self._monitor_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=f"order-monitor-{strategy_id}"
        )
        self._monitor_pool_closed = False
        self._monitor_pool_finalizer = weakref.finalize(
            self, OrderExecutor._shutdown_executor, self._monitor_pool
        )

        # Таймауты
        self._market_timeout_sec = 45
        self._close_retry_attempts = 3
        self._close_retry_backoff_sec = 0.25
        self._cancel_order_timeout_sec = max(
            float(get_setting("cancel_order_timeout_sec", 3.0) or 3.0),
            0.1,
        )

        # Текущий reservation key (для отмены при ошибке до submit)
        self._reservation_counter = 0
        self._reservation_counter_lock = threading.Lock()
        self._submission_lock = threading.Lock()
        self._submission_block_ttl_sec = max(
            float(get_setting("submission_block_ttl_sec", 300.0) or 300.0),
            1.0,
        )
        self._blocked_submission_keys: dict[str, dict[str, float | str]] = {}

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    def _shutdown_executor(executor: ThreadPoolExecutor):
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception:
            pass

    def _cleanup_monitor_pool(self):
        if self._monitor_pool_closed:
            return
        self._monitor_pool_closed = True
        self._shutdown_executor(self._monitor_pool)
        if hasattr(self, "_monitor_pool_finalizer") and self._monitor_pool_finalizer.alive:
            self._monitor_pool_finalizer.detach()

    def _account_has_position(self, ticker: str) -> bool:
        from core.position_manager import position_manager

        for pos in position_manager.get_positions(self._account_id):
            if pos.get("ticker") != ticker:
                continue
            qty = float(pos.get("quantity", 0) or 0)
            if qty != 0:
                return True
        return False

    def _check_account_risk_limits(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> Optional[str]:
        """Проверяет account-level risk limits (cross-strategy).

        Returns:
            None — ордер разрешён.
            str  — причина отклонения.
        """
        ticker = ticker or self._ticker
        board = board or self._board
        max_gross = float(get_setting("max_gross_exposure", 0) or 0)
        max_positions = int(float(get_setting("max_account_positions", 0) or 0))

        if max_gross <= 0 and max_positions <= 0:
            return None  # лимиты не настроены

        if max_gross > 0:
            current_exposure = _get_account_gross_exposure(self._account_id)
            new_order_cost = self._calc_reservation_amount(
                action, qty, ticker=ticker, board=board, last_price=last_price
            )
            total = current_exposure + new_order_cost
            if total > max_gross:
                return (
                    f"gross_exposure {total:.2f} превысит лимит "
                    f"{max_gross:.2f} (текущая={current_exposure:.2f}, "
                    f"новый ордер={new_order_cost:.2f})"
                )

        if max_positions > 0:
            current_count = _get_account_positions_count(self._account_id)
            if not self._account_has_position(ticker):
                if current_count >= max_positions:
                    return (
                        f"количество позиций {current_count} достигло лимита "
                        f"{max_positions}"
                    )

        return None

    def _next_reservation_key(self) -> str:
        with self._reservation_counter_lock:
            self._reservation_counter += 1
            return f"{self._strategy_id}:{self._ticker}:{self._reservation_counter}"

    def _make_submission_key(self, signal: dict, action: str, qty: int, comment: str) -> str:
        explicit = str(signal.get("idempotency_key", "") or "")
        if explicit:
            return explicit
        price = signal.get("price", "")
        return ":".join([
            self._strategy_id,
            self._ticker,
            self._board,
            self._order_mode,
            str(action),
            str(qty),
            str(price),
            str(comment),
        ])

    def _is_submission_blocked(self, submission_key: str) -> bool:
        expired_keys = []
        with self._submission_lock:
            now = time.monotonic()
            expired_keys = self._cleanup_blocked_submissions_unsafe(now)
            blocked = submission_key in self._blocked_submission_keys
        for expired_key in expired_keys:
            logger.info(
                f"[{self._strategy_id}] SUBMIT BLOCK EXPIRED: idempotency_key={expired_key}"
            )
        return blocked

    def _block_submission(self, submission_key: str, reason: str = "ambiguous_submit") -> None:
        if not submission_key:
            return
        with self._submission_lock:
            now = time.monotonic()
            self._blocked_submission_keys[submission_key] = {
                "blocked_at": now,
                "expires_at": now + self._submission_block_ttl_sec,
                "reason": str(reason or "unknown"),
            }

    def _release_submission(self, submission_key: str) -> None:
        if not submission_key:
            return
        with self._submission_lock:
            self._blocked_submission_keys.pop(submission_key, None)

    def _cleanup_blocked_submissions_unsafe(self, now: float) -> list[str]:
        expired = [
            key
            for key, meta in self._blocked_submission_keys.items()
            if float(meta.get("expires_at", 0.0) or 0.0) <= now
        ]
        for key in expired:
            self._blocked_submission_keys.pop(key, None)
        return expired

    def _has_active_pending_orders(self) -> bool:
        for lifecycle in pending_order_registry.get_pending():
            if lifecycle.strategy_id != self._strategy_id:
                continue
            if lifecycle.ticker != self._ticker:
                continue
            if not lifecycle.is_terminal:
                return True
        return False

    def release_blocked_submissions_after_reconcile(self) -> int:
        """Снимает stale submission blocks после успешного reconcile без активных pending orders."""
        if self._has_active_pending_orders():
            return 0

        with self._submission_lock:
            released_keys = list(self._blocked_submission_keys.keys())
            self._blocked_submission_keys.clear()

        if released_keys:
            runtime_metrics.emit_audit_event(
                "submission_block_released",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                reason="reconcile_clear",
                released_count=len(released_keys),
            )
            logger.info(
                f"[{self._strategy_id}] SUBMIT BLOCK RELEASED after reconcile: "
                f"count={len(released_keys)}"
            )
        return len(released_keys)

    @staticmethod
    def _coerce_market_data_envelope(payload) -> MarketDataEnvelope | None:
        if isinstance(payload, MarketDataEnvelope):
            return payload
        if isinstance(payload, dict):
            required = {"source_ts", "receive_ts", "age_ms", "source_id", "status"}
            if required.issubset(payload.keys()):
                return MarketDataEnvelope(
                    source_ts=float(payload.get("source_ts", 0.0) or 0.0),
                    receive_ts=float(payload.get("receive_ts", 0.0) or 0.0),
                    age_ms=int(payload.get("age_ms", 0) or 0),
                    source_id=str(payload.get("source_id", "") or ""),
                    status=str(payload.get("status", "unknown") or "unknown"),
                )
        return None

    @staticmethod
    def _normalize_optional_price(value) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _validate_market_price(price: float, bid: float = 0.0, ask: float = 0.0) -> tuple[bool, str]:
        if price <= 0:
            return False, "non_positive_price"
        if bid < 0 or ask < 0:
            return False, "negative_bid_ask"
        if bid > 0 and ask > 0 and bid > ask:
            return False, "crossed_bid_ask"
        if bid > 0 and ask == 0:
            return False, "missing_ask"
        return True, ""

    @staticmethod
    def _signal_latency_budget_sec() -> float:
        try:
            return float(get_setting("signal_latency_budget_sec", 10.0) or 10.0)
        except (TypeError, ValueError):
            return 10.0

    @staticmethod
    def _stale_quote_budget_ms() -> int:
        try:
            return int(float(get_setting("stale_quote_budget_ms", 5000) or 5000))
        except (TypeError, ValueError):
            return 5000

    @staticmethod
    def _market_data_clock_drift_budget_ms() -> int:
        try:
            return int(float(get_setting("market_data_clock_drift_budget_ms", 1500) or 1500))
        except (TypeError, ValueError):
            return 1500

    @staticmethod
    def _normalize_market_phase_name(value: object) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return text.replace("-", "_").replace(" ", "_")

    def _relaxed_stale_quote_phases(self) -> set[str]:
        configured = get_setting(
            "stale_quote_relaxed_phases",
            ["opening_auction", "closing_auction", "discrete_auction", "pre_clearing"],
        )
        if isinstance(configured, str):
            raw_values = configured.split(",")
        elif isinstance(configured, (list, tuple, set)):
            raw_values = list(configured)
        else:
            raw_values = []
        return {
            normalized
            for normalized in (self._normalize_market_phase_name(item) for item in raw_values)
            if normalized
        }

    def _extract_market_phase(self, signal: dict, market_data: object) -> str:
        for candidate in (
            signal.get("market_phase"),
            signal.get("trading_phase"),
            signal.get("session_phase"),
        ):
            normalized = self._normalize_market_phase_name(candidate)
            if normalized:
                return normalized

        if isinstance(market_data, dict):
            for key in ("market_phase", "trading_phase", "session_phase", "phase"):
                normalized = self._normalize_market_phase_name(market_data.get(key))
                if normalized:
                    return normalized
        return ""

    def _cross_validate_market_data_envelope(
        self,
        envelope: MarketDataEnvelope,
        action: str,
        qty: int,
    ) -> dict[str, int | list[str]]:
        now = time.time()
        receive_ts = float(envelope.receive_ts or 0.0)
        source_ts = float(envelope.source_ts or 0.0)
        payload_age_ms = max(int((receive_ts - source_ts) * 1000), 0) if receive_ts > 0 and source_ts > 0 else int(envelope.age_ms or 0)
        local_age_ms = max(int((now - receive_ts) * 1000), 0) if receive_ts > 0 else int(envelope.age_ms or 0)

        anomalies: list[str] = []
        drift_budget_ms = self._market_data_clock_drift_budget_ms()
        if receive_ts > now + 1.0:
            anomalies.append("receive_ts_in_future")
        if source_ts > receive_ts + 1.0:
            anomalies.append("source_ts_after_receive_ts")
        if abs(payload_age_ms - int(envelope.age_ms or 0)) > drift_budget_ms:
            anomalies.append("age_ms_drift")

        if anomalies:
            runtime_metrics.increment("market_data_timestamp_anomaly")
            runtime_metrics.emit_audit_event(
                "market_data_timestamp_anomaly",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                action=action,
                qty=qty,
                source_id=envelope.source_id,
                status=envelope.status,
                source_ts=source_ts,
                receive_ts=receive_ts,
                age_ms=envelope.age_ms,
                local_age_ms=local_age_ms,
                payload_age_ms=payload_age_ms,
                anomalies=list(anomalies),
            )
            logger.warning(
                f"[{self._strategy_id}] MARKET DATA TIMESTAMP ANOMALY: "
                f"anomalies={','.join(anomalies)} source_id={envelope.source_id} "
                f"status={envelope.status} local_age_ms={local_age_ms} payload_age_ms={payload_age_ms}"
            )

        return {
            "local_age_ms": local_age_ms,
            "payload_age_ms": payload_age_ms,
            "anomalies": anomalies,
        }

    def _cancel_order_result_with_timeout(self, order_id: str) -> tuple[OrderResult | None, bool]:
        state: dict[str, object] = {"result": None, "error": None}
        finished = threading.Event()

        def _worker():
            try:
                state["result"] = self._connector.cancel_order_result(order_id, self._account_id)
            except Exception as exc:
                state["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(
            target=_worker,
            name=f"cancel-order-{self._strategy_id}-{order_id}",
            daemon=True,
        )
        worker.start()
        if not finished.wait(self._cancel_order_timeout_sec):
            return None, True
        error = state["error"]
        if error is not None:
            raise error
        return state["result"], False

    def _escalate_uncertain_limit_cancel(
        self,
        order_id: str,
        side: str,
        qty: int,
        reason: str,
        lifecycle: OrderLifecycle,
    ) -> None:
        runtime_metrics.emit_audit_event(
            "manual_intervention_required",
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            reason=reason,
            order_id=str(order_id),
            order_type="limit_price_cancel",
            side=side,
            qty=qty,
            filled_qty=lifecycle.filled_qty,
            status=lifecycle.state.value,
        )
        logger.error(
            f"[{self._strategy_id}] LIMIT CANCEL UNCERTAIN: tid={order_id} reason={reason} "
            f"filled={lifecycle.filled_qty}/{qty}. Требуется ручное вмешательство."
        )
        if self._on_manual_intervention:
            try:
                self._on_manual_intervention(reason)
            except Exception as exc:
                logger.warning(
                    f"[{self._strategy_id}] Ошибка on_manual_intervention({reason}): {exc}"
                )

    def _get_sec_info(self, ticker: str = "", board: str = "") -> dict | None:
        ticker = ticker or self._ticker
        board = board or self._board
        if hasattr(self._connector, "get_sec_info"):
            try:
                return self._connector.get_sec_info(ticker, board)
            except Exception:
                return None
        return None

    def _get_instrument_constraints(self, ticker: str = "", board: str = ""):
        return build_constraints(self._get_sec_info(ticker, board), lot_size=1)

    def _normalize_order_qty(self, qty: int, ticker: str = "", board: str = "") -> int:
        return normalize_quantity(qty, self._get_instrument_constraints(ticker, board))

    def _normalize_submit_price(self, price: float, ticker: str = "", board: str = "") -> float:
        return normalize_price(price, self._get_instrument_constraints(ticker, board))

    def _calc_reservation_amount(
        self,
        side: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> float:
        """Рассчитывает сумму капитала, которую заблокирует ордер."""
        ticker = ticker or self._ticker
        board = board or self._board
        sec_info = self._get_sec_info(ticker, board)

        go = 0.0
        if sec_info:
            go = float(
                sec_info.get("buy_deposit" if side == "buy" else "sell_deposit") or 0
            )

        if go > 0:
            return qty * go

        price = last_price or 0.0
        if price <= 0:
            if ticker == self._ticker and board == self._board:
                price = self._get_last_price()
            elif hasattr(self._connector, "get_last_price"):
                try:
                    price = self._connector.get_last_price(ticker, board) or 0.0
                except Exception:
                    price = 0.0
        constraints = build_constraints(sec_info, lot_size=1)
        return normalize_notional(price, qty, constraints)

    def check_account_risk_limits_for_order(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> Optional[str]:
        return self._check_account_risk_limits(
            action=action,
            qty=qty,
            ticker=ticker,
            board=board,
            last_price=last_price,
        )

    def reserve_capital_for_order(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> str:
        reservation_key = self._next_reservation_key()
        reservation_amount = self._calc_reservation_amount(
            action,
            qty,
            ticker=ticker,
            board=board,
            last_price=last_price,
        )
        if reservation_amount > 0:
            reservation_ledger.reserve(reservation_key, self._account_id, reservation_amount)
            return reservation_key
        return ""

    def release_reserved_capital(self, reservation_key: str):
        if reservation_key:
            reservation_ledger.release(reservation_key)

    def stop(self):
        """Остановить executor и отменить активные chase-ордера."""
        self._running = False
        with self._submission_lock:
            self._blocked_submission_keys.clear()
        self._cleanup_monitor_pool()
        with self._chase_lock:
            active_chases = list(self._active_chase_orders)
            self._active_chase_orders.clear()

        for chase in active_chases:
            if not chase.is_done:
                chase.cancel()
                chase.wait(timeout=5)

    def _handle_failure(self):
        """Регистрирует ошибку и вызывает on_circuit_break при срабатывании."""
        if self._risk_guard.record_failure():
            logger.error(
                f"[{self._strategy_id}] CIRCUIT BREAKER: "
                f"порог ошибок достигнут — вызов аварийного обработчика"
            )
            if self._on_circuit_break:
                try:
                    self._on_circuit_break()
                except Exception as e:
                    logger.error(
                        f"[{self._strategy_id}] Ошибка в on_circuit_break: {e}"
                    )

    def _send_strategy_alert(self, description: str) -> None:
        try:
            notifier.send(
                EventCode.STRATEGY_ERROR,
                agent=self._strategy_id,
                description=description,
            )
        except Exception:
            pass

    # --- Публичные методы исполнения ---

    def execute_signal(self, signal: dict):
        """Исполняет торговый сигнал.

        order_mode='market'      — рыночная заявка.
        order_mode='limit'       — лимитка по лучшей цене в стакане (ChaseOrder).
        order_mode='limit_price' — лимитка по цене из сигнала (signal["price"]).
        """
        action = signal.get("action")
        qty = signal.get("qty", 1)

        # Валидация qty
        try:
            qty = self._normalize_order_qty(int(qty))
            if qty <= 0:
                logger.error(
                    f"[{self._strategy_id}] Некорректный qty={qty} в сигнале, должен быть > 0"
                )
                self._handle_failure()
                return
        except (TypeError, ValueError):
            logger.error(
                f"[{self._strategy_id}] Некорректный тип qty={qty} в сигнале, ожидается число"
            )
            self._handle_failure()
            return

        comment = signal.get("comment", "")
        submission_key = self._make_submission_key(signal, action, qty, comment)

        if self._is_submission_blocked(submission_key):
            logger.warning(
                f"[{self._strategy_id}] SUBMIT BLOCKED: duplicate idempotency_key={submission_key} "
                f"для action={action} qty={qty}. Требуется ручная проверка предыдущего submit."
            )
            return

        # Динамический лот
        if action in ("buy", "sell") and self._lot_sizing.get("dynamic"):
            dyn_qty = self._calc_dynamic_qty(action)
            if dyn_qty is not None:
                qty = dyn_qty
                logger.info(f"[{self._strategy_id}] Динамический лот: {qty}")
            else:
                logger.warning(
                    f"[{self._strategy_id}] Недостаточно средств для {action} "
                    f"(свободных средств: {self._connector.get_free_money(self._account_id)}), "
                    f"сигнал пропущен"
                )
                return

        fill_price = self._get_last_price()
        fill_price_text = f"{fill_price:.4f}" if fill_price else "н/д"
        raw_market_data = signal.get("market_data_envelope") or self._get_last_price_envelope()
        envelope = self._coerce_market_data_envelope(raw_market_data)
        market_phase = self._extract_market_phase(signal, raw_market_data)
        bid = self._normalize_optional_price(signal.get("bid"))
        ask = self._normalize_optional_price(signal.get("ask"))

        signal_ts = self._normalize_optional_price(signal.get("signal_ts"))
        if signal_ts > 0:
            signal_age_sec = max(time.time() - signal_ts, 0.0)
            latency_budget_sec = self._signal_latency_budget_sec()
            if signal_age_sec > latency_budget_sec and not bool(signal.get("allow_stale_signal", False)):
                runtime_metrics.increment("stale_signal_reject")
                runtime_metrics.emit_audit_event(
                    "stale_data_reject",
                    strategy_id=self._strategy_id,
                    ticker=self._ticker,
                    reason="stale_signal",
                    action=action,
                    qty=qty,
                    age_ms=round(signal_age_sec * 1000.0, 3),
                )
                logger.warning(
                    f"[{self._strategy_id}] STALE SIGNAL REJECT: age={signal_age_sec:.3f}s "
                    f"> budget={latency_budget_sec:.3f}s action={action} qty={qty}"
                )
                self._send_strategy_alert(
                    f"STALE SIGNAL REJECT: {action.upper()} {self._ticker} x{qty} age={signal_age_sec:.3f}s"
                )
                return
            if signal_age_sec > latency_budget_sec:
                comment = f"{comment} [stale_signal]".strip()

        if action in ("buy", "sell"):
            is_valid_price, price_error = self._validate_market_price(fill_price, bid=bid, ask=ask)
            if not is_valid_price:
                runtime_metrics.increment("market_data_reject")
                runtime_metrics.emit_audit_event(
                    "stale_data_reject",
                    strategy_id=self._strategy_id,
                    ticker=self._ticker,
                    reason=price_error,
                    action=action,
                    qty=qty,
                )
                logger.warning(
                    f"[{self._strategy_id}] MARKET DATA REJECT: {price_error} "
                    f"action={action} qty={qty} price={fill_price_text} bid={bid} ask={ask}"
                )
                self._send_strategy_alert(
                    f"MARKET DATA REJECT: {price_error} {action.upper()} {self._ticker} x{qty}"
                )
                return
            if envelope is not None:
                stale_quote_budget_ms = self._stale_quote_budget_ms()
                clock_check = self._cross_validate_market_data_envelope(envelope, action, qty)
                local_quote_age_ms = int(clock_check["local_age_ms"])
                payload_quote_age_ms = int(clock_check["payload_age_ms"])
                relaxed_phase = market_phase in self._relaxed_stale_quote_phases()
                should_reject_stale_quote = local_quote_age_ms > stale_quote_budget_ms and not relaxed_phase
                if envelope.status == "degraded" or should_reject_stale_quote:
                    reject_reason = "degraded_quote" if envelope.status == "degraded" else "stale_quote"
                    runtime_metrics.increment("stale_quote_reject")
                    runtime_metrics.emit_audit_event(
                        "stale_data_reject",
                        strategy_id=self._strategy_id,
                        ticker=self._ticker,
                        reason=reject_reason,
                        action=action,
                        qty=qty,
                        age_ms=local_quote_age_ms,
                        payload_age_ms=payload_quote_age_ms,
                        status=envelope.status,
                        source_id=envelope.source_id,
                        market_phase=market_phase,
                    )
                    logger.warning(
                        f"[{self._strategy_id}] STALE QUOTE REJECT: local_age_ms={local_quote_age_ms} "
                        f"payload_age_ms={payload_quote_age_ms} "
                        f"status={envelope.status} budget_ms={stale_quote_budget_ms} "
                        f"market_phase={market_phase or 'unknown'} "
                        f"action={action} qty={qty}"
                    )
                    self._send_strategy_alert(
                        f"STALE QUOTE REJECT: {action.upper()} {self._ticker} x{qty} age_ms={local_quote_age_ms}"
                    )
                    return
                if relaxed_phase and local_quote_age_ms > stale_quote_budget_ms:
                    runtime_metrics.emit_audit_event(
                        "stale_quote_phase_override",
                        strategy_id=self._strategy_id,
                        ticker=self._ticker,
                        action=action,
                        qty=qty,
                        market_phase=market_phase,
                        age_ms=local_quote_age_ms,
                        payload_age_ms=payload_quote_age_ms,
                        status=envelope.status,
                        source_id=envelope.source_id,
                    )
                    logger.info(
                        f"[{self._strategy_id}] STALE QUOTE OVERRIDE: phase={market_phase} "
                        f"local_age_ms={local_quote_age_ms} budget_ms={stale_quote_budget_ms}"
                    )

        # === Pre-trade risk gate ===
        if action in ("buy", "sell"):
            # Circuit breaker — запрещаем открывающие ордера
            if self._risk_guard.is_circuit_open():
                logger.warning(
                    f"[{self._strategy_id}] RISK REJECT: circuit breaker открыт, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                if self._on_circuit_break:
                    try:
                        self._on_circuit_break()
                    except Exception as e:
                        logger.warning(
                            f"[{self._strategy_id}] Ошибка on_circuit_break при RISK REJECT: {e}"
                        )
                return

            # Лимиты риска (max_position_size, daily_loss_limit)
            allowed, reason = self._risk_guard.check_risk_limits(action, qty, ticker=self._ticker)
            if not allowed:
                logger.warning(
                    f"[{self._strategy_id}] RISK REJECT: {reason}, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                return

            # Account-level risk limits (cross-strategy)
            account_reject = self._check_account_risk_limits(action, qty)
            if account_reject:
                logger.warning(
                    f"[{self._strategy_id}] ACCOUNT RISK REJECT: {account_reject}, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                return

        if signal_ts > 0:
            runtime_metrics.record_latency(
                "signal_to_submit_ms",
                max(time.time() - signal_ts, 0.0) * 1000.0,
            )

        res_key = ""
        try:
            if action in ("buy", "sell"):
                # Атомарная проверка: нет позиции + нет ордера → ставим in-flight
                if not self._position_tracker.try_set_order_in_flight():
                    state = self._position_tracker.get_state()
                    if state["position"] != 0:
                        logger.warning(
                            f"[{self._strategy_id}] Позиция уже открыта "
                            f"({state['position']}, qty={state['position_qty']}), "
                            f"игнорируем {action.upper()} цена~{fill_price_text}"
                        )
                    else:
                        logger.warning(
                            f"[{self._strategy_id}] Ордер уже в работе, "
                            f"игнорируем {action.upper()} цена~{fill_price_text}"
                        )
                    return

                # Резервируем капитал под pending-ордер
                res_key = self._next_reservation_key()
                res_amount = self._calc_reservation_amount(action, qty)
                if res_amount > 0:
                    reservation_ledger.reserve(res_key, self._account_id, res_amount)

                if self._order_mode == "limit":
                    self._execute_chase(action, qty, comment, reservation_key=res_key)
                elif self._order_mode == "limit_price":
                    price = self._normalize_submit_price(
                        self._normalize_optional_price(signal.get("price")) or fill_price
                    )
                    if price <= 0:
                        self._position_tracker.clear_order_in_flight()
                        reservation_ledger.release(res_key)
                        runtime_metrics.increment("market_data_reject")
                        logger.warning(
                            f"[{self._strategy_id}] LIMIT PRICE REJECT: non_positive_normalized_price "
                            f"action={action} qty={qty} raw_price={signal.get('price')}"
                        )
                        return
                    self._execute_limit_price(
                        action, qty, comment, price, reservation_key=res_key,
                        submission_key=submission_key,
                    )
                else:
                    self._execute_market(
                        action, qty, comment, fill_price, reservation_key=res_key,
                        submission_key=submission_key,
                    )

            elif action == "close":
                if self._order_mode in ("limit", "limit_price"):
                    # Атомарная проверка: есть позиция + нет ордера → ставим in-flight
                    if not self._position_tracker.try_set_order_in_flight_for_close():
                        state = self._position_tracker.get_state()
                        if state["position"] == 0:
                            logger.warning(
                                f"[{self._strategy_id}] Нет открытой позиции, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        else:
                            logger.warning(
                                f"[{self._strategy_id}] Лимитный ордер уже в работе, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        return
                else:
                    # Market-mode: тоже используем guarded check
                    if not self._position_tracker.try_set_order_in_flight_for_close():
                        state = self._position_tracker.get_state()
                        if state["position"] == 0:
                            logger.warning(
                                f"[{self._strategy_id}] Нет открытой позиции, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        else:
                            logger.warning(
                                f"[{self._strategy_id}] Ордер уже в работе, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        return

                pos = self._position_tracker.get_position()
                close_side = "sell" if pos == 1 else "buy"
                close_qty = abs(self._position_tracker.get_position_qty())

                if self._order_mode == "limit":
                    self._execute_chase(close_side, close_qty, comment, is_close=True)
                elif self._order_mode == "limit_price":
                    price = self._normalize_submit_price(
                        self._normalize_optional_price(signal.get("price")) or fill_price
                    )
                    if price <= 0:
                        self._position_tracker.clear_order_in_flight()
                        runtime_metrics.increment("market_data_reject")
                        logger.warning(
                            f"[{self._strategy_id}] CLOSE LIMIT REJECT: non_positive_normalized_price "
                            f"qty={close_qty} raw_price={signal.get('price')}"
                        )
                        return
                    self._execute_limit_price(
                        close_side, close_qty, comment, price, is_close=True,
                        submission_key=submission_key,
                    )
                else:
                    self._execute_market_close(
                        close_side, close_qty, comment, fill_price, submission_key=submission_key
                    )

        except Exception as e:
            self._position_tracker.clear_order_in_flight()
            if action in ("buy", "sell"):
                reservation_ledger.release(res_key)
            self._release_submission(submission_key)
            logger.error(f"[{self._strategy_id}] Ошибка исполнения {action}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # --- Рыночные ордера ---

    def _execute_market(self, side: str, qty: int, comment: str, fill_price: float,
                        reservation_key: str = "", submission_key: str = ""):
        """Рыночная заявка на открытие позиции."""
        submit_started = time.monotonic()
        runtime_metrics.increment("submit_attempt")
        order_result = self._connector.place_order_result(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="market",
            board=self._board,
            agent_name=self._agent_name,
        )
        runtime_metrics.record_latency(
            "submit_to_ack_ms",
            (time.monotonic() - submit_started) * 1000.0,
        )
        tid = order_result.transaction_id
        if tid:
            if reservation_key:
                reservation_ledger.bind_order(reservation_key, str(tid))
            self._release_submission(submission_key)
            runtime_metrics.increment("submit_success")
            lifecycle = OrderLifecycle(
                tid=str(tid),
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                side=side,
                requested_qty=qty,
                order_type="market",
                correlation_id=submission_key,
            )
            runtime_metrics.emit_audit_event(
                "order_submitted",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                order_id=str(tid),
                order_type="market",
                side=side,
                qty=qty,
                reservation_key=reservation_key,
            )
            pending_order_registry.register(lifecycle)
            self._risk_guard.notify_order_submitted(side, ticker=self._ticker)
            self._risk_guard.record_success()
            logger.info(
                f"[{self._strategy_id}] MARKET {side.upper()} x{qty} "
                f"@ {fill_price:.4f} tid={tid} (мониторинг...)"
            )
            self._monitor_pool.submit(
                self._monitor_market_order,
                tid, side, qty, fill_price, comment, False, reservation_key, lifecycle,
            )
        else:
            if order_result.outcome in {OrderOutcome.STALE_STATE, OrderOutcome.TRANSPORT_ERROR}:
                self._block_submission(submission_key, reason="ambiguous_submit")
                runtime_metrics.increment("stale_state_count")
                runtime_metrics.emit_audit_event(
                    "manual_intervention_required",
                    strategy_id=self._strategy_id,
                    ticker=self._ticker,
                    reason="ambiguous_submit",
                    order_type="market",
                    side=side,
                    qty=qty,
                    reservation_key=reservation_key,
                )
                if reservation_key:
                    reservation_ledger.mark_stale(reservation_key, "ambiguous_submit")
                if self._on_manual_intervention:
                    self._on_manual_intervention("ambiguous_submit")
            else:
                reservation_ledger.release(reservation_key)
            self._handle_failure()
            runtime_metrics.increment("submit_reject")
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена={fill_price} вид=market | {comment} "
                f"outcome={order_result.outcome.value} msg={order_result.message}"
            )
            self._position_tracker.clear_order_in_flight()

    def _execute_market_close(
        self, close_side: str, close_qty: int, comment: str, fill_price: float,
        submission_key: str = "",
    ):
        """Рыночное закрытие позиции.

        Использует connector.close_position() как каноническую точку входа.
        SAFETY: при неуспехе НЕ делает fallback на place_order.
        Вместо этого помечает стратегию как требующую ручного вмешательства
        и уведомляет оператора.
        """
        submit_started = time.monotonic()
        runtime_metrics.increment("submit_attempt")
        close_result = self._submit_close_with_retry(close_qty, fill_price)
        tid = close_result.transaction_id if close_result else None
        runtime_metrics.record_latency(
            "submit_to_ack_ms",
            (time.monotonic() - submit_started) * 1000.0,
        )

        if tid:
            self._release_submission(submission_key)
            runtime_metrics.increment("submit_success")
            lifecycle = OrderLifecycle(
                tid=str(tid),
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                side=close_side,
                requested_qty=close_qty,
                order_type="market",
                correlation_id=submission_key,
            )
            runtime_metrics.emit_audit_event(
                "order_submitted",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                order_id=str(tid),
                order_type="market_close",
                side=close_side,
                qty=close_qty,
            )
            pending_order_registry.register(lifecycle)
            self._risk_guard.notify_order_submitted("close", ticker=self._ticker)
            self._risk_guard.record_success()
            logger.info(
                f"[{self._strategy_id}] CLOSE MARKET {close_side.upper()} x{close_qty} "
                f"@ {fill_price:.4f} tid={tid} (мониторинг...)"
            )
            self._monitor_pool.submit(
                self._monitor_market_order,
                tid, close_side, close_qty, fill_price, comment, True, "", lifecycle,
            )
            return

        # close_position не удался — НЕ делаем fallback, требуем ручное вмешательство
        self._handle_failure()
        if close_result is None or close_result.outcome in {OrderOutcome.STALE_STATE, OrderOutcome.TRANSPORT_ERROR}:
            self._block_submission(submission_key, reason="ambiguous_submit")
            runtime_metrics.increment("stale_state_count")
            runtime_metrics.emit_audit_event(
                "manual_intervention_required",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                reason="close_failed",
                order_type="market_close",
                side=close_side,
                qty=close_qty,
            )
        logger.error(
            f"[{self._strategy_id}] CLOSE FAILED: close_position вернул None. "
            f"сторона={close_side.upper()} qty={close_qty} "
            f"цена~{fill_price:.4f} | {comment}. "
            f"Требуется ручное вмешательство (manual_intervention_required)."
        )
        self._position_tracker.clear_order_in_flight()
        if self._on_manual_intervention:
            try:
                self._on_manual_intervention("close_failed")
            except Exception as e:
                logger.warning(
                    f"[{self._strategy_id}] Ошибка on_manual_intervention(close_failed): {e}"
                )
        try:
            notifier.send(
                EventCode.STRATEGY_CRASHED,
                agent=self._strategy_id,
                description=(
                    f"[{self._strategy_id}] CLOSE FAILED: невозможно закрыть позицию "
                    f"{close_side.upper()} {self._ticker} x{close_qty}. "
                    f"Требуется ручное вмешательство."
                ),
            )
        except Exception:
            pass

    def _submit_close_with_retry(self, close_qty: int, fill_price: float):
        last_result = None
        for attempt in range(1, self._close_retry_attempts + 1):
            try:
                result = self._connector.close_position_result(
                    account_id=self._account_id,
                    ticker=self._ticker,
                    quantity=close_qty,
                    agent_name=self._agent_name,
                )
            except Exception as exc:
                logger.warning(
                    f"[{self._strategy_id}] close attempt {attempt}/{self._close_retry_attempts} "
                    f"raised: {exc}, цена~{fill_price:.4f}"
                )
                result = None
            else:
                if result and result.transaction_id:
                    return result
                if result and result.outcome not in {OrderOutcome.STALE_STATE, OrderOutcome.TRANSPORT_ERROR}:
                    return result
                logger.warning(
                    f"[{self._strategy_id}] close attempt {attempt}/{self._close_retry_attempts} "
                    f"ambiguous: outcome={getattr(result.outcome, 'value', 'none')} "
                    f"msg={getattr(result, 'message', '')}"
                )

            last_result = result
            if attempt >= self._close_retry_attempts:
                break
            time.sleep(self._close_retry_backoff_sec * attempt)

        return last_result

    def _monitor_market_order(
        self, tid: str, side: str, qty: int, price: float, comment: str, is_close: bool,
        reservation_key: str = "", lifecycle: OrderLifecycle | None = None,
    ) -> bool:
        """Мониторинг рыночного ордера до подтверждения исполнения."""
        _TERMINAL = {
            "matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"
        }
        TIMEOUT_SEC = self._market_timeout_sec

        lifecycle = lifecycle or OrderLifecycle(
            tid=str(tid),
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            side=side,
            requested_qty=qty,
            order_type="market",
        )

        logger.debug(
            f"[{self._strategy_id}] Мониторинг MARKET tid={tid} "
            f"{side.upper()} x{qty} @ {price:.4f}"
        )

        monitor_started = time.monotonic()
        confirmed = False
        deadline = time.monotonic() + TIMEOUT_SEC
        timeout_reached = False

        while self._running and time.monotonic() < deadline:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[{self._strategy_id}] get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")
                avg_price = info.get("avg_price") or info.get("price")

                filled = 0
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                try:
                    avg_p = float(avg_price) if avg_price else 0.0
                except (TypeError, ValueError):
                    avg_p = 0.0

                lifecycle.update_from_connector(status, filled, avg_p)
                pending_order_registry.refresh(lifecycle)

                if avg_p > 0:
                    price = avg_p

                if status == "matched":
                    confirmed = True
                    logger.info(
                        f"[{self._strategy_id}] MARKET tid={tid} "
                        f"исполнен filled={lifecycle.filled_qty}/{qty} @ {price:.4f}"
                    )
                    break
                elif status in _TERMINAL:
                    logger.info(
                        f"[{self._strategy_id}] MARKET tid={tid} завершён "
                        f"статус={status} filled={lifecycle.filled_qty}/{qty} @ {price:.4f}"
                    )
                    break

            time.sleep(0.5)

        if not confirmed and time.monotonic() >= deadline:
            timeout_reached = True
            lifecycle.mark_timeout()
            pending_order_registry.refresh(lifecycle)

        filled = lifecycle.filled_qty
        trade_to_record = None
        partial_trade = None
        notify_payload = None

        with self._position_tracker._position_lock:
            if filled > 0 and confirmed:
                self._position_tracker.close_position(filled, qty) if is_close else None
                if not is_close:
                    self._position_tracker.confirm_open(side, filled, price)
                trade_to_record = (side, filled, price, comment, str(tid))
                notify_payload = (side, filled, qty, price, comment)
                self._position_tracker.clear_order_in_flight()
                success = True
            else:
                if filled > 0:
                    if is_close:
                        self._position_tracker.close_position(filled, qty)
                    else:
                        self._position_tracker.confirm_open(side, filled, price)
                    partial_trade = (side, filled, price, comment, str(tid))
                self._position_tracker.clear_order_in_flight()
                success = False

        # Освобождаем резерв капитала (ордер завершён: fill/cancel/timeout)
        if reservation_key:
            reservation_ledger.release(reservation_key)

        if trade_to_record:
            side_r, filled_r, price_r, comment_r, tid_r = trade_to_record
            self._trade_recorder.record_trade(
                side_r, filled_r, price_r, comment_r, order_type="market", order_ref=tid_r,
                correlation_id=lifecycle.correlation_id,
            )
            runtime_metrics.increment("orders_filled")
            runtime_metrics.record_latency(
                "ack_to_fill_ms",
                (time.monotonic() - monitor_started) * 1000.0,
            )
            logger.info(
                f"[{self._strategy_id}] MARKET подтверждено: "
                f"{side_r.upper()} filled={filled_r}/{qty} @ {price_r}"
            )
            if notify_payload:
                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=(
                            f"{notify_payload[0].upper()} {self._ticker} "
                            f"x{notify_payload[1]} @ {notify_payload[3]} "
                            f"[market] | {notify_payload[4]}"
                        ),
                    )
                except Exception:
                    pass
            return True

        if partial_trade:
            side_p, filled_p, price_p, comment_p, tid_p = partial_trade
            self._trade_recorder.record_trade(
                side_p, filled_p, price_p, comment_p, order_type="market", order_ref=tid_p,
                correlation_id=lifecycle.correlation_id,
            )
            runtime_metrics.increment("orders_partial_fill")
            runtime_metrics.record_latency(
                "ack_to_fill_ms",
                (time.monotonic() - monitor_started) * 1000.0,
            )
            logger.info(
                f"[{self._strategy_id}] MARKET частично: "
                f"{side_p.upper()} filled={filled_p}/{qty} @ {price_p:.4f}"
            )
            return True

        if timeout_reached:
            logger.warning(
                f"[{self._strategy_id}] MARKET tid={tid} таймаут {TIMEOUT_SEC} сек, reconcile..."
            )
            if self._on_reconcile:
                try:
                    self._on_reconcile()
                except Exception as e:
                    logger.warning(f"[{self._strategy_id}] reconcile error: {e}")
        return success

    # --- Лимитные ордера по фиксированной цене ---

    def _execute_limit_price(
        self, side: str, qty: int, comment: str, price: float, is_close: bool = False,
        reservation_key: str = "", submission_key: str = "",
    ):
        """Лимитная заявка по фиксированной цене из сигнала."""
        submit_started = time.monotonic()
        runtime_metrics.increment("submit_attempt")
        order_result = self._connector.place_order_result(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="limit",
            price=price,
            board=self._board,
            agent_name=self._agent_name,
        )
        runtime_metrics.record_latency(
            "submit_to_ack_ms",
            (time.monotonic() - submit_started) * 1000.0,
        )
        tid = order_result.transaction_id
        if not tid:
            if order_result.outcome in {OrderOutcome.STALE_STATE, OrderOutcome.TRANSPORT_ERROR}:
                self._block_submission(submission_key, reason="ambiguous_submit")
                runtime_metrics.increment("stale_state_count")
                runtime_metrics.emit_audit_event(
                    "manual_intervention_required",
                    strategy_id=self._strategy_id,
                    ticker=self._ticker,
                    reason="ambiguous_submit",
                    order_type="limit_price",
                    side=side,
                    qty=qty,
                    reservation_key=reservation_key,
                )
                if reservation_key:
                    reservation_ledger.mark_stale(reservation_key, "ambiguous_submit")
                if self._on_manual_intervention:
                    self._on_manual_intervention("ambiguous_submit")
            else:
                reservation_ledger.release(reservation_key)
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена={price} вид=limit_price | {comment} "
                f"outcome={order_result.outcome.value} msg={order_result.message}"
            )
            self._handle_failure()
            runtime_metrics.increment("submit_reject")
            self._position_tracker.clear_order_in_flight()
            return

        self._release_submission(submission_key)
        if reservation_key:
            reservation_ledger.bind_order(reservation_key, str(tid))
        runtime_metrics.increment("submit_success")

        logger.info(
            f"[{self._strategy_id}] LIMIT {side.upper()} x{qty} @ {price} tid={tid} ({comment})"
        )

        lifecycle = OrderLifecycle(
            tid=str(tid),
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            side=side,
            requested_qty=qty,
            order_type="limit",
            correlation_id=submission_key,
        )
        runtime_metrics.emit_audit_event(
            "order_submitted",
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            order_id=str(tid),
            order_type="limit_price",
            side=side,
            qty=qty,
            price=price,
            reservation_key=reservation_key,
        )
        pending_order_registry.register(lifecycle)
        self._risk_guard.notify_order_submitted("close" if is_close else side, ticker=self._ticker)

        self._monitor_pool.submit(
            self._monitor_limit_price_order,
            tid, side, qty, price, comment, is_close, reservation_key, lifecycle,
        )

    def _monitor_limit_price_order(
        self, tid: str, side: str, qty: int, price: float, comment: str, is_close: bool,
        reservation_key: str = "", lifecycle: OrderLifecycle | None = None,
    ):
        """Фоновый мониторинг лимитной заявки по фиксированной цене."""
        _TERMINAL = {
            "matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"
        }
        CANCEL_TIME_MIN = TRADING_END_TIME_MIN

        lifecycle = lifecycle or OrderLifecycle(
            tid=str(tid),
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            side=side,
            requested_qty=qty,
            order_type="limit",
        )

        cancelled_by_time = False

        logger.debug(
            f"[{self._strategy_id}] Мониторинг LIMIT tid={tid} "
            f"{side.upper()} x{qty} @ {price}"
        )

        monitor_started = time.monotonic()
        cancel_outcome_uncertain = False
        while self._running:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[{self._strategy_id}] get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")

                filled = 0
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                avg_price = info.get("avg_price") or info.get("price") or 0.0
                try:
                    avg_p = float(avg_price)
                except (TypeError, ValueError):
                    avg_p = 0.0

                lifecycle.update_from_connector(status, filled, avg_p)
                pending_order_registry.refresh(lifecycle)

                if status in _TERMINAL:
                    logger.info(
                        f"[{self._strategy_id}] LIMIT tid={tid} "
                        f"статус={status} filled={lifecycle.filled_qty}/{qty}"
                    )
                    break

            now_min = datetime.now().hour * 60 + datetime.now().minute
            if now_min >= CANCEL_TIME_MIN:
                logger.info(
                    f"[{self._strategy_id}] LIMIT tid={tid} "
                    f"снимается по времени 23:45 (filled={lifecycle.filled_qty}/{qty})"
                )
                lifecycle.mark_cancel_pending()
                pending_order_registry.refresh(lifecycle)
                try:
                    cancel_result, cancel_timed_out = self._cancel_order_result_with_timeout(tid)
                    if cancel_timed_out:
                        cancel_outcome_uncertain = True
                        self._escalate_uncertain_limit_cancel(
                            tid,
                            side,
                            qty,
                            "cancel_timeout",
                            lifecycle,
                        )
                        break
                    if not cancel_result.is_success:
                        logger.warning(
                            f"[{self._strategy_id}] cancel_order tid={tid}: "
                            f"outcome={cancel_result.outcome.value} msg={cancel_result.message}"
                        )
                        if cancel_result.outcome in {
                            OrderOutcome.STALE_STATE,
                            OrderOutcome.TRANSPORT_ERROR,
                        }:
                            cancel_outcome_uncertain = True
                            self._escalate_uncertain_limit_cancel(
                                tid,
                                side,
                                qty,
                                "cancel_uncertain",
                                lifecycle,
                            )
                            break
                except Exception as e:
                    logger.warning(f"[{self._strategy_id}] cancel_order tid={tid}: {e}")
                cancelled_by_time = True
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    try:
                        info2 = self._connector.get_order_status(tid)
                        if info2:
                            st2 = info2.get("status", "")
                            b2 = info2.get("balance")
                            q2 = info2.get("quantity")
                            avg2 = info2.get("avg_price") or info2.get("price") or 0.0
                            filled2 = 0
                            if b2 is not None and q2 is not None:
                                filled2 = int(q2) - int(b2)
                            try:
                                avg2 = float(avg2)
                            except (TypeError, ValueError):
                                avg2 = 0.0
                            lifecycle.update_from_connector(st2, filled2, avg2)
                            pending_order_registry.refresh(lifecycle)
                            if st2 in _TERMINAL:
                                break
                    except Exception:
                        pass
                break

            time.sleep(1.0)

        filled = lifecycle.filled_qty

        if cancel_outcome_uncertain:
            if reservation_key:
                reservation_ledger.mark_stale(reservation_key, "cancel_uncertain")
            pending_order_registry.refresh(lifecycle)
            return

        with self._position_tracker._position_lock:
            self._position_tracker.clear_order_in_flight()
            if filled > 0:
                if is_close:
                    self._position_tracker.close_position(filled, qty)
                else:
                    self._position_tracker.confirm_open(side, filled, price)

                self._trade_recorder.record_trade(
                    side, filled, price, comment, order_type="limit", order_ref=str(tid),
                    correlation_id=lifecycle.correlation_id,
                )
                runtime_metrics.increment("orders_filled")
                runtime_metrics.record_latency(
                    "ack_to_fill_ms",
                    (time.monotonic() - monitor_started) * 1000.0,
                )
                logger.info(
                    f"[{self._strategy_id}] LIMIT исполнена: "
                    f"{side.upper()} filled={filled}/{qty} @ {price} "
                    f"{'(снята по времени, частично)' if cancelled_by_time and filled < qty else ''}"
                )

                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=(
                            f"{side.upper()} {self._ticker} x{filled} @ {price} "
                            f"[limit_price] | {comment}"
                        ),
                    )
                except Exception:
                    pass

            else:
                if cancelled_by_time:
                    logger.info(
                        f"[{self._strategy_id}] LIMIT tid={tid} снята в 23:45, не исполнена"
                    )
                else:
                    logger.warning(
                        f"[{self._strategy_id}] LIMIT tid={tid} завершена без исполнения"
                    )

        # Освобождаем резерв капитала
        if reservation_key:
            reservation_ledger.release(reservation_key)

    # --- Chase-ордера ---

    def _execute_chase(self, side: str, qty: int, comment: str, is_close: bool = False,
                       reservation_key: str = ""):
        """Лимитная заявка через ChaseOrder (стакан)."""
        import time as _time
        chase_ref = (
            f"chase:{self._strategy_id}:{self._ticker}:{side}:{int(_time.time() * 1000)}"
        )
        if reservation_key:
            reservation_ledger.bind_order(reservation_key, chase_ref)
        runtime_metrics.emit_audit_event(
            "order_submitted",
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            order_id=chase_ref,
            order_type="chase",
            side=side,
            qty=qty,
            reservation_key=reservation_key,
        )
        self._risk_guard.notify_order_submitted("close" if is_close else side, ticker=self._ticker)

        chase_price = self._get_last_price() or 0.0
        chase_price_text = f"{chase_price:.4f}" if chase_price else "bid/offer"
        logger.info(
            f"[{self._strategy_id}] Chase {side.upper()} x{qty} "
            f"цена~{chase_price_text} ({comment}) — фоновый поток"
        )

        chase = ChaseOrder(
            connector=self._connector,
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            board=self._board,
            agent_name=self._agent_name,
        )

        with self._chase_lock:
            self._active_chase_orders.append(chase)

        def _run():
            try:
                chase.wait(timeout=120)

                filled_qty = chase.filled_qty
                target_qty = qty
                fill_rate = (filled_qty / target_qty * 100) if target_qty > 0 else 0

                if fill_rate < 50:
                    logger.warning(
                        f"[{self._strategy_id}] Частичное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%) "
                        f"цена~{chase.avg_price:.4f}"
                    )
                elif fill_rate < 100:
                    logger.info(
                        f"[{self._strategy_id}] Неполное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%) "
                        f"цена~{chase.avg_price:.4f}"
                    )
            finally:
                with self._chase_lock:
                    if chase in self._active_chase_orders:
                        self._active_chase_orders.remove(chase)

                if not chase.is_done:
                    chase.cancel()

                self._on_chase_done(chase, side, qty, comment, is_close, chase_ref,
                                    reservation_key)

        self._monitor_pool.submit(_run)

    def _on_chase_done(
        self, chase, side: str, qty: int, comment: str, is_close: bool, chase_ref: str = "",
        reservation_key: str = "",
    ):
        """Вызывается из фонового потока после завершения ChaseOrder."""
        if not self._running:
            logger.warning(
                f"[{self._strategy_id}] _on_chase_done пропущен — engine остановлен"
            )
            if reservation_key:
                reservation_ledger.release(reservation_key)
            return

        filled = chase.filled_qty
        avg_px = chase.avg_price

        if filled <= 0:
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена=bid/offer "
                f"вид=limit(стакан) — ничего не исполнено за 60 сек | {comment}"
            )
            self._handle_failure()
            self._position_tracker.clear_order_in_flight()
            if reservation_key:
                reservation_ledger.release(reservation_key)
            return

        if is_close:
            self._position_tracker.close_position(filled, qty)
        else:
            self._position_tracker.confirm_open(side, filled, avg_px)

        self._position_tracker.clear_order_in_flight()

        # Освобождаем резерв капитала
        if reservation_key:
            reservation_ledger.release(reservation_key)

        logger.info(
            f"[{self._strategy_id}] Запись chase-ордера в history: "
            f"exec_key={chase_ref}, side={side}, filled={filled}, avg_px={avg_px}"
        )
        self._trade_recorder.record_trade(
            side, filled, avg_px, comment, order_type="chase", order_ref=chase_ref,
            correlation_id=chase_ref,
        )
        runtime_metrics.increment("orders_filled")
        self._risk_guard.record_success()

        logger.info(
            f"[{self._strategy_id}] Chase done: {side.upper()} "
            f"filled={filled}/{qty} avg={avg_px:.4f} ({comment})"
        )

        try:
            notifier.send(
                EventCode.ORDER_FILLED,
                agent=self._strategy_id,
                description=(
                    f"{side.upper()} {self._ticker} x{filled} @ {avg_px:.4f} "
                    f"[chase] | {comment}"
                ),
            )
        except Exception:
            pass

    # --- Динамический расчёт лота ---

    def _calc_dynamic_qty(self, side: str) -> Optional[int]:
        """Рассчитывает динамический лот.

        Формула: Floor((available_money / (drawdown + GO)) / instances)

        available_money = free_money - уже зарезервированный капитал
        по другим pending-ордерам на этом же account_id.
        """
        free_money = self._connector.get_free_money(self._account_id)
        if free_money is None or free_money <= 0:
            return None

        available_money = reservation_ledger.available(self._account_id, free_money)
        if available_money <= 0:
            logger.debug(
                f"[{self._strategy_id}] dynamic qty: free={free_money:.2f} "
                f"reserved={free_money - available_money:.2f} available=0"
            )
            return None

        sec_info = None
        if hasattr(self._connector, "get_sec_info"):
            sec_info = self._connector.get_sec_info(self._ticker, self._board)

        go = 0.0
        if sec_info:
            go = float(
                sec_info.get("buy_deposit" if side == "buy" else "sell_deposit") or 0
            )

        manual_dd = float(self._lot_sizing.get("drawdown", 0))
        strat_dd = 0  # get_max_drawdown вызывается извне
        effective_dd = max(manual_dd, strat_dd)

        instances = max(int(self._lot_sizing.get("instances", 1)), 1)

        if go <= 0:
            price = self._get_last_price()
            if price <= 0:
                return int(self._lot_sizing.get("lot", 1)) or 1

            lot_size = int(sec_info.get("lotsize", 1)) if sec_info else 1
            position_cost = price * lot_size

            if position_cost <= 0:
                return int(self._lot_sizing.get("lot", 1)) or 1

            qty = math.floor(available_money / position_cost / instances)
            return qty if qty >= 1 else None

        denom = effective_dd + go
        if denom <= 0:
            return None

        qty = math.floor((available_money / denom) / instances)
        return qty if qty >= 1 else None

    def check_pending_late_fills(self) -> list[dict]:
        """Проверяет pending ордера на late fills.

        Вызывается из reconcile path или dedicated checker.
        Late fills записываются через TradeRecorder как repair event.

        Returns:
            Список обнаруженных late fills.
        """
        late_fills = pending_order_registry.check_late_fills(self._connector)

        for lf in late_fills:
            if lf["strategy_id"] != self._strategy_id:
                continue

            delta = lf["delta"]
            side = lf["side"]
            avg_price = lf["avg_price"]
            tid = lf["tid"]

            logger.warning(
                f"[{self._strategy_id}] LATE FILL REPAIR: "
                f"tid={tid} {side.upper()} +{delta} fills @ {avg_price:.4f}"
            )

            # Записываем дополнительные fills как repair event
            repair_ref = f"late_fill:{tid}:{delta}"
            try:
                self._trade_recorder.record_trade(
                    side, delta, avg_price,
                    f"late_fill_repair tid={tid}",
                    order_type="market",
                    order_ref=repair_ref,
                    correlation_id=lf["lifecycle"].correlation_id or repair_ref,
                )
            except Exception as e:
                logger.error(
                    f"[{self._strategy_id}] LATE FILL REPAIR failed: {e}"
                )

            # Убираем из реестра после обработки
            pending_order_registry.unregister(tid)

        return late_fills
