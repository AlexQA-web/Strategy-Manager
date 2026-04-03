# core/backtest_engine.py

import statistics
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from loguru import logger

from core.txt_loader import Bar, TXTLoader
from core.commission_manager import commission_manager


@dataclass
class Trade:
    direction: int
    qty: int
    entry_dt: datetime
    entry_price: float
    entry_comment: str
    exit_dt: datetime = None
    exit_price: float = None
    exit_comment: str = ""
    gross_pnl: float = 0.0
    commission: float = 0.0
    net_pnl: float = 0.0
    point_cost: float = 1.0  # для расчёта комиссии фьючерсов

    @property
    def is_closed(self) -> bool:
        return self.exit_dt is not None


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[tuple[datetime, float]]
    bars_count: int
    ticker: str
    date_from: datetime
    date_to: datetime
    total_net_pnl: float = 0.0
    total_gross_pnl: float = 0.0
    total_commission: float = 0.0
    trades_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    recovery_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0


class BacktestEngine:
    """
    Прогон стратегии по историческим барам.
    Сигнал на close[i] → исполнение по open[i+1].
    Индикаторы pre-calculated через pandas — O(n).
    """

    def __init__(self, loader: TXTLoader | None = None):
        self._loader = loader or TXTLoader()

    def run(self, module, filepath: str, connector_id: str = "finam", 
            board: str = "TQBR", stop_flag=None) -> BacktestResult:
        self.stop_flag = stop_flag or (lambda: False)
        
        # Сохраняем для передачи в _close_trade
        self._connector_id = connector_id
        self._board = board

        raw_params = module.get_params() if hasattr(module, "get_params") else {}
        params = {k: v["default"] for k, v in raw_params.items()}
        
        # Поддержка режима "auto" для комиссии
        commission_param = params.get("commission", 0.0)
        commission_mode = "auto" if commission_param == "auto" else "manual"
        commission_value = 0.0 if commission_mode == "auto" else float(commission_param)
        # Получаем order_mode для определения роли ордера (maker/taker)
        order_mode = params.get("order_mode", "market")
        # Параметр slippage (проскальзывание) — доля от цены исполнения
        slippage = float(params.get("slippage", 0.0))

        bars = self._loader.load(filepath)
        if len(bars) < 2:
            raise ValueError("Недостаточно баров (минимум 2)")

        logger.info(
            f"Бэктест: {bars[0].ticker} | "
            f"{bars[0].dt.date()} → {bars[-1].dt.date()} | "
            f"{len(bars):,} баров"
        )

        df = pd.DataFrame([{
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "vol": b.vol, "dt": b.dt, "date_int": b.date_int,
            "time_min": b.time_min, "weekday": b.weekday,
        } for b in bars])

        # Делегируем pre-calc в стратегию, если та реализует on_precalc
        df = self._precalc_indicators(df, params, module)

        bar_dicts = df.to_dict("records")

        position = 0
        open_trade: Trade | None = None
        trades: list[Trade] = []
        equity_curve: list[tuple[datetime, float]] = []
        cumulative_pnl = 0.0

        # Стратегия определяет lookback через get_lookback(params), иначе — вся история
        if callable(getattr(module, "get_lookback", None)):
            lookback = module.get_lookback(params)
        else:
            lookback = 0  # 0 = передавать всю историю

        for i in range(len(bars) - 1):
            if self.stop_flag():
                logger.info("Бэктест прерван пользователем")
                raise InterruptedError("Бэктест остановлен")

            current_bar = bars[i]
            next_bar    = bars[i + 1]
            history     = bar_dicts[max(0, i + 1 - lookback): i + 1] if lookback > 0 else bar_dicts[:i + 1]

            try:
                signal = module.on_bar(history, position, params)
            except Exception as e:
                logger.warning(f"on_bar ошибка на баре {i}: {e}")
                signal = {"action": None}
            action = signal.get("action")

            if action is not None:
                base_price = next_bar.open
                exec_dt    = next_bar.dt
                # Применяем проскальзывание к цене исполнения
                # Для close передаём направление открытой позиции
                direction = open_trade.direction if open_trade is not None else 0
                exec_price = self._apply_slippage(base_price, action, slippage, direction)

                if action == "close" and open_trade is not None:
                    trade = self._close_trade(
                        open_trade, exec_price, exec_dt,
                        signal.get("comment", ""), commission_mode, commission_value,
                        bars[0].ticker, bars[0].board, order_mode
                    )
                    trades.append(trade)
                    cumulative_pnl += trade.net_pnl
                    open_trade = None
                    position   = 0
                    logger.debug(f" CLOSE {exec_dt} @ {exec_price:.4f} | net: {trade.net_pnl:+.2f}")

                elif action == "buy" and position == 0:
                    # Получаем point_cost из модуля стратегии, если доступно
                    point_cost = getattr(module, "point_cost", 1.0) or 1.0
                    open_trade = Trade(
                        direction=+1, qty=signal.get("qty", 1),
                        entry_dt=exec_dt, entry_price=exec_price,
                        entry_comment=signal.get("comment", ""),
                        point_cost=point_cost,
                    )
                    position = +1
                    logger.debug(f" BUY  {exec_dt} @ {exec_price:.4f}")

                elif action == "sell" and position == 0:
                    # Получаем point_cost из модуля стратегии, если доступно
                    point_cost = getattr(module, "point_cost", 1.0) or 1.0
                    open_trade = Trade(
                        direction=-1, qty=signal.get("qty", 1),
                        entry_dt=exec_dt, entry_price=exec_price,
                        entry_comment=signal.get("comment", ""),
                        point_cost=point_cost,
                    )
                    position = -1
                    logger.debug(f" SELL {exec_dt} @ {exec_price:.4f}")

            equity_curve.append((current_bar.dt, cumulative_pnl))

        if open_trade is not None:
            last  = bars[-1]
            # Применяем slippage к forced close
            exit_price = self._apply_slippage(last.close, "close", slippage, open_trade.direction)
            trade = self._close_trade(
                open_trade, exit_price, last.dt,
                "Force close (end of data)", commission_mode, commission_value,
                bars[0].ticker, bars[0].board, order_mode
            )
            trades.append(trade)
            cumulative_pnl += trade.net_pnl
            logger.warning(f"Позиция закрыта на конце данных @ {last.close:.4f}")

        result = BacktestResult(
            trades=trades, equity_curve=equity_curve,
            bars_count=len(bars), ticker=bars[0].ticker,
            date_from=bars[0].dt, date_to=bars[-1].dt,
        )
        self._calc_metrics(result)

        logger.info(
            f"Готово | сделок: {result.trades_count} | "
            f"win rate: {result.win_rate:.1f}% | "
            f"net P&L: {result.total_net_pnl:+.2f} | "
            f"max DD: {result.max_drawdown:.2f}"
        )
        return result

    @staticmethod
    def _apply_slippage(base_price: float, action: str, slippage: float, direction: int = 0) -> float:
        """
        Применяет проскальзывание к базовой цене исполнения.
        Slippage всегда ухудшает цену исполнения:
        - Для buy (вход в long или закрытие short): цена увеличивается
        - Для sell (вход в short или закрытие long): цена уменьшается
        - Для close long (direction=+1): цена закрытия уменьшается
        - Для close short (direction=-1): цена закрытия увеличивается
        - Для close без direction: fallback к sell (цена уменьшается)
        """
        if slippage <= 0:
            return base_price
        if action == "buy":
            # Покупка — всегда платим больше (вход в long или закрытие short)
            return base_price * (1.0 + slippage)
        elif action == "sell":
            # Продажа — всегда получаем меньше (вход в short или закрытие long)
            return base_price * (1.0 - slippage)
        elif action == "close":
            if direction == +1:
                # Закрываем long = продаём — slippage уменьшает цену выхода
                return base_price * (1.0 - slippage)
            elif direction == -1:
                # Закрываем short = покупаем — slippage увеличивает цену выхода
                return base_price * (1.0 + slippage)
            else:
                # Fallback: close без direction = sell
                return base_price * (1.0 - slippage)
        return base_price

    @staticmethod
    def _precalc_indicators(df: pd.DataFrame, params: dict, module=None) -> pd.DataFrame:
        """
        Если стратегия реализует on_precalc(df, params) → делегируем ей.
        Иначе — возвращаем DataFrame без изменений.
        """
        if module is not None and callable(getattr(module, "on_precalc", None)):
            logger.debug("Pre-calc: делегирование в стратегию (on_precalc)")
            return module.on_precalc(df, params)

        logger.debug("Pre-calc: стратегия не реализует on_precalc, пропуск")
        return df

    def _close_trade(self, trade, exit_price, exit_dt, comment, commission_mode, commission_value, ticker, board="TQBR", order_mode="market") -> Trade:
        trade.exit_price   = exit_price
        trade.exit_dt      = exit_dt
        trade.exit_comment = comment
        trade.gross_pnl    = trade.direction * (exit_price - trade.entry_price) * trade.qty * (trade.point_cost or 1.0)
        
        # Расчёт комиссии
        if commission_mode == "auto":
            try:
                order_role = "maker" if order_mode == "limit" else "taker"
                connector_id = getattr(self, "_connector_id", "finam")
                point_cost = getattr(trade, "point_cost", 1.0) or 1.0
                # Считаем отдельно для входа и выхода — точнее, чем по средней цене
                commission_entry = commission_manager.calculate(
                    ticker=ticker, board=board, quantity=trade.qty,
                    price=trade.entry_price, order_role=order_role,
                    point_cost=point_cost, connector_id=connector_id,
                )
                commission_exit = commission_manager.calculate(
                    ticker=ticker, board=board, quantity=trade.qty,
                    price=exit_price, order_role=order_role,
                    point_cost=point_cost, connector_id=connector_id,
                )
                trade.commission = commission_entry + commission_exit
            except Exception as e:
                logger.warning(f"Ошибка автоматического расчёта комиссии в бэктесте: {e}. Используется 0.")
                trade.commission = 0.0
        else:
            # Ручной режим (обратная совместимость)
            trade.commission = commission_value * trade.qty * 2
        
        trade.net_pnl = trade.gross_pnl - trade.commission
        return trade

    @staticmethod
    def _calc_metrics(result: BacktestResult) -> None:
        trades = [t for t in result.trades if t.is_closed]
        if not trades:
            return

        result.trades_count     = len(trades)
        result.total_gross_pnl  = sum(t.gross_pnl  for t in trades)
        result.total_commission = sum(t.commission  for t in trades)
        result.total_net_pnl    = sum(t.net_pnl     for t in trades)

        wins   = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]

        result.winning_trades = len(wins)
        result.losing_trades  = len(losses)
        result.win_rate  = len(wins) / len(trades) * 100
        result.avg_win   = sum(t.net_pnl for t in wins)   / len(wins)   if wins   else 0.0
        result.avg_loss  = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0

        gross_wins   = sum(t.net_pnl for t in wins)
        gross_losses = abs(sum(t.net_pnl for t in losses))
        result.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        # Max Drawdown
        peak, max_dd = 0.0, 0.0
        for _, v in result.equity_curve:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown     = max_dd
        result.max_drawdown_pct = (max_dd / peak * 100) if peak > 0 else 0.0

        result.recovery_factor = (
            result.total_net_pnl / result.max_drawdown
            if result.max_drawdown > 0 else float("inf")
        )

        # ── Sharpe по дневной доходности equity curve ─────────────────────
        # Группируем equity по дням, берём последнее значение каждого дня
        daily: dict = {}
        for dt, val in result.equity_curve:
            daily[dt.date()] = val

        sorted_days = sorted(daily.keys())
        if len(sorted_days) > 1:
            # Используем процентные доходности для корректного расчёта Sharpe
            daily_returns_pct = []
            for i in range(1, len(sorted_days)):
                prev_val = daily[sorted_days[i - 1]]
                curr_val = daily[sorted_days[i]]
                if prev_val > 0:
                    daily_returns_pct.append((curr_val - prev_val) / prev_val)
                else:
                    daily_returns_pct.append(0.0)
            if len(daily_returns_pct) > 1:
                avg = statistics.mean(daily_returns_pct)
                std = statistics.stdev(daily_returns_pct)
                result.sharpe_ratio = (avg / std) * (252 ** 0.5) if std > 0 else 0.0
