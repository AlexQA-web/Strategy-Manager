import threading
import time
import re
from typing import Optional
from loguru import logger

from core.base_connector import BaseConnector
from core.storage import get_setting
from core.moex_api import MOEXClient

CONNECTOR_ID = "quik"


class QuikConnector(BaseConnector):
    """
    Коннектор QUIK через QuikPy (cia76).
    Требует запущенного QUIK с Lua-скриптом QuikSharp.lua.
    https://github.com/cia76/QuikPy
    """

    def __init__(self):
        super().__init__()
        self._connected = False
        self._client    = None
        self._lock      = threading.Lock()
        self.moex_client = MOEXClient()

    # ── Подключение ─────────────────────────────────────────────────────

    def connect(self) -> bool:
        host           = get_setting("quik_host")           or "127.0.0.1"
        requests_port  = int(get_setting("quik_port")       or 34130)
        callbacks_port = int(get_setting("quik_callbacks_port") or 34131)

        logger.info(f"[QUIK] Подключение → {host}:{requests_port}")
        try:
            from QuikPy import QuikPy
            with self._lock:
                self._client = QuikPy(
                    host=host,
                    requests_port=requests_port,
                    callbacks_port=callbacks_port,
                )
            # Проверяем связь с брокерским сервером
            result = self._client.is_connected()
            if result.get("data") == 1:
                self._connected = True
                self._stop_reconnect.clear()
                self.start_reconnect_loop()  # Запускаем мониторинг реконнекта
                logger.info("[QUIK] ✅ Подключён к серверу брокера")
                self._fire(self._on_connect)
                return True
            else:
                logger.warning("[QUIK] Lua-скрипт работает, но терминал не подключён к серверу брокера")
                # Соединение с Lua есть — считаем подключённым (торговать нельзя, но работать можно)
                self._connected = True
                self._stop_reconnect.clear()
                self.start_reconnect_loop()  # Запускаем мониторинг реконнекта
                self._fire(self._on_connect)
                return True

        except ConnectionRefusedError:
            msg = (f"Не удалось подключиться к {host}:{requests_port}. "
                   f"Убедись что QUIK запущен и скрипт QuikSharp.lua активен.")
            logger.error(f"[QUIK] {msg}")
            self._fire(self._on_error, msg)
            return False
        except Exception as e:
            logger.error(f"[QUIK] Exception: {e}")
            self._fire(self._on_error, str(e))
            return False

    def disconnect(self):
        self._stop_reconnect.set()
        client = self._client
        self._client    = None
        self._connected = False
        if client:
            try:
                # close_connection_and_thread() — штатный метод QuikPy:
                # закрывает socket_requests + устанавливает callback_exit_event
                # чтобы callback_thread завершился
                client.close_connection_and_thread()
            except Exception as e:
                logger.warning(f"[QUIK] disconnect error: {e}")
                # Принудительно закрываем сокет если штатный метод упал
                try:
                    client.socket_requests.close()
                except Exception:
                    pass
                try:
                    client.callback_exit_event.set()
                except Exception:
                    pass
        logger.info("[QUIK] Отключён")
        self._fire(self._on_disconnect)

    def is_connected(self) -> bool:
        """Проверяет состояние подключения к QUIK.
        
        Использует кэшированное состояние _connected, но также проверяет что
        _client существует. Реальная проверка соединения (ping) выполняется
        в фоновом потоке reconnect_loop.
        """
        # Проверяем не только флаг, но и что клиент жив
        if not self._connected or self._client is None:
            return False
        return True

    def ping(self) -> bool:
        """Реальная проверка соединения через легкий запрос.
        
        Используется reconnect_loop для определения реального состояния.
        """
        if not self._connected or self._client is None:
            return False
        try:
            # Используем is_connected() — легкий запрос без параметров
            with self._lock:
                result = self._client.is_connected()
            return result.get("data") == 1
        except Exception:
            return False

    # ── Ордера ──────────────────────────────────────────────────────────

    def place_order(
        self,
        account_id: str,
        ticker: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        price: float = 0.0,
        board: str = "TQBR",
        agent_name: str = "",
    ) -> Optional[str]:
        if not self._connected or not self._client:
            logger.warning("[QUIK] place_order — нет подключения")
            return None
        try:
            transaction = {
                "ACTION":    "NEW_ORDER",
                "CLASSCODE": board,
                "SECCODE":   ticker,
                "ACCOUNT":   account_id,
                "CLIENT":    account_id,
                "OPERATION": "B" if side == "buy" else "S",
                "PRICE":     str(price) if order_type == "limit" else "0",
                "QUANTITY":  str(quantity),
                "TYPE":      "L" if order_type == "limit" else "M",
                "COMMENT":   agent_name,
            }
            with self._lock:
                result = self._client.send_transaction(transaction)
            trans_id = str(result.get("data", ""))
            logger.info(f"[QUIK] Ордер {side} {ticker}x{quantity} board={board}: transID={trans_id}")
            return trans_id or None
        except Exception as e:
            logger.error(f"[QUIK] place_order error: {e}")
            self._fire(self._on_error, str(e))
            return None

    def cancel_order(self, order_id: str, account_id: str) -> bool:
        if not self._connected or not self._client:
            return False
        try:
            transaction = {
                "ACTION":   "KILL_ORDER",
                "TRANS_ID": order_id,
                "ACCOUNT":  account_id,
            }
            with self._lock:
                self._client.send_transaction(transaction)
            return True
        except Exception as e:
            logger.error(f"[QUIK] cancel_order error: {e}")
            return False

    def close_position(
        self,
        account_id: str,
        ticker: str,
        quantity: int = 0,
        agent_name: str = "",
    ) -> bool:
        positions = self.get_positions(account_id)
        pos = next((p for p in positions if p.get("ticker") == ticker), None)
        if not pos:
            return False
        pos_qty = int(pos.get("quantity", 0))
        # Если quantity = 0, закрываем всю позицию, иначе - частично
        close_qty = abs(quantity) if quantity != 0 else abs(pos_qty)
        side = "sell" if pos_qty > 0 else "buy"
        result = self.place_order(
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=close_qty,
            order_type="market",
            board=pos.get("board", "TQBR"),
            agent_name=agent_name,
        )
        return result is not None

    # ── Позиции / счета ─────────────────────────────────────────────────

    def _resolve_trade_acc(self, account_id: str) -> set[str]:
        """Возвращает множество trdaccid, соответствующих account_id (client_code или trdaccid).
        Нужно потому что в UI используется client_code (10U9QR), а в depo_limits поле trdaccid (L00+000023EC).
        """
        ids = {account_id}
        try:
            quik_accounts = getattr(self._client, "accounts", [])
            for acc in quik_accounts:
                cc  = acc.get("client_code", "")
                trd = acc.get("trade_account_id", "")
                if cc == account_id and trd:
                    ids.add(trd)
                elif trd == account_id and cc:
                    ids.add(cc)
        except Exception:
            pass
        return ids

    def get_positions(self, account_id: str) -> list[dict]:
        """Позиции по счёту. account_id может быть client_code (10U9QR) или trdaccid.
        Резолвим оба варианта через _resolve_trade_acc чтобы не терять позиции.
        """
        if not self._connected or not self._client:
            return []
        try:
            trade_ids = self._resolve_trade_acc(account_id)
            with self._lock:
                result = self._client.get_all_depo_limits()
            items = result.get("data", [])
            if not isinstance(items, list):
                return []
            positions = []
            for item in items:
                item_trd = item.get("trdaccid", "")
                item_cc  = item.get("client_code", "")
                if item_trd not in trade_ids and item_cc not in trade_ids:
                    continue
                qty = float(item.get("currentbal", 0))
                if qty == 0:
                    continue
                positions.append({
                    "ticker":        item.get("sec_code", ""),
                    "board":         item.get("class_code", "TQBR"),
                    "quantity":      qty,
                    "avg_price":     float(item.get("awg_position_price", 0)),
                    "current_price": 0.0,  # получается отдельным запросом
                    "pnl":           0.0,
                })
            return positions
        except Exception as e:
            logger.error(f"[QUIK] get_positions error: {e}")
            return []

    def get_accounts(self) -> list[dict]:
        """Возвращает список union/клиентских счетов (client_code, напр. 10U9QR).
        Использует self._client.accounts — структуру, которую QuikPy строит при инициализации
        из get_trade_accounts() + get_money_limits(). Каждый элемент содержит client_code
        (человекочитаемый union-счёт) и trade_account_id (внутренний trdaccid типа L00+000023EC).
        В UI показываем client_code, его же передаём как account_id во все операции.
        """
        if not self._connected or not self._client:
            return []
        try:
            # Получаем money_limits один раз — используем для резолвинга client_code
            with self._lock:
                ml_result = self._client.get_money_limits()
            money_limits = ml_result.get("data", []) or []
            if not isinstance(money_limits, list):
                money_limits = []

            # QuikPy при инициализации строит self.accounts со всеми нужными полями
            quik_accounts = getattr(self._client, "accounts", [])
            if quik_accounts:
                seen = set()
                result = []
                for acc in quik_accounts:
                    client_code = acc.get("client_code", "")
                    trade_acc   = acc.get("trade_account_id", "")
                    firm_id     = acc.get("firm_id", "")

                    # Если client_code пустой — ищем в money_limits по trdaccid или по firmid
                    if not client_code and trade_acc:
                        cc = next((m.get("client_code", "") for m in money_limits
                                   if m.get("trdaccid") == trade_acc and m.get("client_code")), "")
                        if not cc:
                            cc = next((m.get("client_code", "") for m in money_limits
                                       if m.get("firmid") == firm_id and m.get("client_code")), "")
                        if cc:
                            client_code = cc

                    if client_code and client_code not in seen:
                        seen.add(client_code)
                        label = f"{client_code} ({firm_id})" if firm_id else client_code
                        result.append({
                            "id":            client_code,
                            "name":          label,
                            "trade_acc_id":  trade_acc,
                        })
                    # trade_acc без client_code не добавляем — это субсчёт того же клиента

                # Дополняем из get_client_codes() — там могут быть счета (напр. ИИС),
                # которых нет в trade_accounts/money_limits
                try:
                    with self._lock:
                        cc_result = self._client.get_client_codes()
                    codes = cc_result.get("data", [])
                    if isinstance(codes, str) and codes:
                        codes = [c.strip() for c in codes.split(",") if c.strip()]
                    if isinstance(codes, list):
                        for code in codes:
                            if code and code not in seen:
                                seen.add(code)
                                result.append({"id": code, "name": code, "trade_acc_id": ""})
                except Exception as ex:
                    logger.warning(f"[QUIK] get_client_codes supplement error: {ex}")

                if result:
                    return result

        except Exception as e:
            logger.warning(f"[QUIK] get_accounts error: {e}")

        # fallback — только get_client_codes()
        try:
            with self._lock:
                result = self._client.get_client_codes()
            codes = result.get("data", [])
            if isinstance(codes, list) and codes:
                return [{"id": code, "name": code} for code in codes if code]
            if isinstance(codes, str) and codes:
                return [{"id": c.strip(), "name": c.strip()} for c in codes.split(",") if c.strip()]
        except Exception as e:
            logger.warning(f"[QUIK] get_client_codes error: {e}")

        logger.error("[QUIK] get_accounts: не удалось получить счета")
        return []

    # ── Список инструментов ─────────────────────────────────────────────

    def get_classes(self) -> list[str]:
        if not self._connected or not self._client:
            return []
        try:
            with self._lock:
                result = self._client.get_classes_list()
            raw = result.get("data", "")
            return [c.strip() for c in raw.split(",") if c.strip()]
        except Exception as e:
            logger.error(f"[QUIK] get_classes error: {e}")
            return []

    def get_securities(self, board: str = "TQBR") -> list[dict]:
        if not self._connected or not self._client:
            return []
        try:
            with self._lock:
                result = self._client.get_class_securities(board)
            raw     = result.get("data", "")
            tickers = [t.strip() for t in raw.split(",") if t.strip()]
            securities = []
            for ticker in tickers:
                securities.append({
                    "ticker": ticker,
                    "name":   "",   # имя грузится отдельно — дорого для всего списка
                    "board":  board,
                })
            return securities
        except Exception as e:
            logger.error(f"[QUIK] get_securities error: {e}")
            return []

    def get_history(self, ticker: str, board: str,
                    period: str, days: int) -> Optional["pd.DataFrame"]:
        """Загружает историю свечей через QUIK DataSource (get_candles_from_data_source)."""
        if not self._connected or not self._client:
            return None
        try:
            import pandas as pd
            from datetime import datetime, timedelta

            interval_map = {
                "1m": 1, "5m": 5, "15m": 15, "30m": 30,
                "1h": 60, "4h": 240, "1d": 1440,
            }
            interval = interval_map.get(period, 15)

            with self._lock:
                result = self._client.get_candles_from_data_source(
                    board, ticker, interval, count=0
                )
            candles = result.get("data", [])

            if not candles:
                logger.warning(f"[QUIK] get_history: нет свечей {ticker} {period}")
                return None

            rows = []
            for c in candles:
                try:
                    dt_raw = c.get("datetime", {})
                    dt = datetime(
                        year=int(dt_raw.get("year", 2000)),
                        month=int(dt_raw.get("month", 1)),
                        day=int(dt_raw.get("day", 1)),
                        hour=int(dt_raw.get("hour", 0)),
                        minute=int(dt_raw.get("min",
                                              dt_raw.get("minute", 0))),
                        second=int(dt_raw.get("sec",
                                              dt_raw.get("second", 0))),
                    )
                    rows.append({
                        "datetime": dt,
                        "Open": float(c.get("open", 0)),
                        "High": float(c.get("high", 0)),
                        "Low": float(c.get("low", 0)),
                        "Close": float(c.get("close", 0)),
                        "Volume": float(c.get("volume", 0)),
                    })
                except Exception as e:
                    logger.debug(f"[QUIK] свеча пропущена: {e}")
                    continue

            if not rows:
                return None

            df = pd.DataFrame(rows)
            df.set_index("datetime", inplace=True)
            df.sort_index(inplace=True)

            cutoff = datetime.now() - timedelta(days=days)
            df = df[df.index >= cutoff]

            logger.debug(f"[QUIK] get_history {ticker} {period}: {len(df)} свечей")
            return df

        except Exception as e:
            logger.error(f"[QUIK] get_history error: {e}")
            return None

    def get_last_price(self, ticker: str, board: str = "TQBR") -> Optional[float]:
        if not self._connected or not self._client:
            return None
        try:
            with self._lock:
                result = self._client.get_param_ex(board, ticker, "LAST")
            value = result.get("data", {}).get("param_value")
            return float(value) if value else None
        except Exception as e:
            logger.warning(f"[QUIK] get_last_price error: {e}")
            return None

    def get_free_money(self, account_id: str) -> Optional[float]:
        """Свободные средства на счёте через get_money_limits.
        account_id может быть client_code (10U9QR) или trdaccid — резолвим оба варианта.
        """
        if not self._connected or not self._client:
            return None
        try:
            trade_ids = self._resolve_trade_acc(account_id)
            with self._lock:
                result = self._client.get_money_limits()
            items = result.get("data", [])
            if not isinstance(items, list):
                return None
            for item in items:
                item_cc  = item.get("client_code", "")
                item_trd = item.get("trdaccid", "")
                if item_cc in trade_ids or item_trd in trade_ids:
                    val = item.get("currentbal") or item.get("currentlimit")
                    if val is not None:
                        return float(val)
        except Exception as e:
            logger.warning(f"[QUIK] get_free_money error: {e}")
        return None

    def _detect_sec_type(self, ticker: str) -> str:
        """Определяет тип инструмента по тикеру.
        
        Фьючерсы обычно содержат:
        - Дефис (например, Si-3.25)
        - Буквенно-цифровой код месяца поставки (например, SiH5, RIU4)
        
        Args:
            ticker: Тикер инструмента
            
        Returns:
            'futures' или 'stock'
        """
        # Проверяем наличие дефиса (характерно для фьючерсов)
        if '-' in ticker:
            return 'futures'
        
        # Проверяем паттерн фьючерсов: буквы + буква месяца + цифра года
        # Например: SiH5, RIU4, BRJ5
        futures_pattern = re.compile(r'^[A-Z]{2,4}[FGHJKMNQUVXZ]\d{1,2}$', re.IGNORECASE)
        if futures_pattern.match(ticker):
            return 'futures'
        
        # По умолчанию считаем акцией
        return 'stock'

    def get_sec_info(self, ticker: str, board: str = "TQBR") -> Optional[dict]:
        """Возвращает информацию по инструменту: buy_deposit, sell_deposit, point_cost.

        Приоритет получения данных:
        1. MOEX API (через MOEXClient)
        2. QUIK API (fallback)

        QUIK: STEPPRICE = стоимость минимального шага цены за 1 контракт.
        point_cost нормализуется: STEPPRICE / MINSTEP = стоимость одного пункта.
        LiveEngine использует: PnL = (price - entry_price) * qty * point_cost.
        """
        if not self._connected or not self._client:
            return None
        
        # Фильтруем невалидные тикеры
        if not ticker or ticker.strip() in ("", "—", "-"):
            return None
        
        try:
            result = {}
            
            # Определяем тип инструмента
            sec_type = self._detect_sec_type(ticker)
            
            # Пытаемся получить данные с MOEX API
            moex_data = None
            try:
                moex_data = self.moex_client.get_instrument_info(ticker, sec_type)
                if moex_data:
                    logger.debug(f"[QUIK] Используются данные MOEX для {ticker} ({sec_type}): "
                                f"minstep={moex_data['minstep']}, point_cost={moex_data['point_cost']}")
            except Exception as e:
                logger.warning(f"[QUIK] Ошибка получения данных MOEX для {ticker}: {e}")
            
            # Получаем BUYDEPO и SELLDEPO (только из QUIK)
            for param, key in (
                ("BUYDEPO",   "buy_deposit"),
                ("SELLDEPO",  "sell_deposit"),
            ):
                with self._lock:
                    r = self._client.get_param_ex(board, ticker, param)
                val = r.get("data", {}).get("param_value")
                result[key] = float(val) if val else 0.0
            
            # Если данные MOEX получены, используем их для point_cost
            if moex_data:
                result["point_cost"] = moex_data["point_cost"]
            else:
                # Fallback: получаем данные через QUIK API
                logger.info(f"[QUIK] Fallback на QUIK API для {ticker}")
                
                # Получаем STEPPRICE (стоимость минимального шага)
                with self._lock:
                    stepprice_result = self._client.get_param_ex(board, ticker, "STEPPRICE")
                stepprice = float(stepprice_result.get("data", {}).get("param_value", 0))
                
                # Получаем MINSTEP (минимальный шаг цены)
                with self._lock:
                    minstep_result = self._client.get_param_ex(board, ticker, "SEC_PRICE_STEP")
                minstep = float(minstep_result.get("data", {}).get("param_value", 1))
                
                # Нормализуем: стоимость ОДНОГО пункта
                if minstep > 0:
                    result["point_cost"] = stepprice / minstep
                else:
                    result["point_cost"] = stepprice
                
                logger.info(f"[QUIK] QUIK API данные для {ticker}: "
                           f"STEPPRICE={stepprice}, MINSTEP={minstep}, point_cost={result['point_cost']}")
            
            return result
        except Exception as e:
            logger.warning(f"[QUIK] get_sec_info error: {e}")
            return None

    def get_best_quote(self, board: str, ticker: str) -> Optional[dict]:
        """Возвращает {"bid": ..., "offer": ..., "last": ...} через get_param_ex."""
        if not self._connected or not self._client:
            return None
        try:
            result = {}
            for param, key in (("BID", "bid"), ("OFFER", "offer"), ("LAST", "last")):
                with self._lock:
                    r = self._client.get_param_ex(board, ticker, param)
                val = r.get("data", {}).get("param_value")
                result[key] = float(val) if val else 0.0
            return result
        except Exception as e:
            logger.warning(f"[QUIK] get_best_quote error: {e}")
            return None

    def subscribe_quotes(self, board: str, ticker: str):
        """QUIK обновляет котировки через get_param_ex без явной подписки — no-op."""
        pass

    def unsubscribe_quotes(self, board: str, ticker: str):
        """no-op для QUIK."""
        pass

    def get_order_book(self, board: str, ticker: str, depth: int = 10) -> Optional[dict]:
        """
        Получить стакан заявок через get_quote_level2.
        
        Returns:
            {"bids": [(price, volume), ...], "asks": [(price, volume), ...]}
            bids отсортированы по убыванию цены, asks по возрастанию
        """
        if not self._connected or not self._client:
            return None
        try:
            with self._lock:
                result = self._client.get_quote_level2(board, ticker)
            
            data = result.get("data", {})
            if not data:
                return None
            
            # Парсим bid и offer из структуры QUIK
            bid_items = data.get("bid", [])
            offer_items = data.get("offer", [])
            
            bids = []
            for item in bid_items[:depth]:
                price = float(item.get("price", 0))
                qty = float(item.get("quantity", 0))
                if price > 0 and qty > 0:
                    bids.append((price, qty))
            
            asks = []
            for item in offer_items[:depth]:
                price = float(item.get("price", 0))
                qty = float(item.get("quantity", 0))
                if price > 0 and qty > 0:
                    asks.append((price, qty))
            
            # Сортируем: bids по убыванию, asks по возрастанию
            bids.sort(reverse=True, key=lambda x: x[0])
            asks.sort(key=lambda x: x[0])
            
            return {"bids": bids, "asks": asks}
            
        except Exception as e:
            logger.warning(f"[QUIK] get_order_book error: {e}")
            return None


quik_connector = QuikConnector()
