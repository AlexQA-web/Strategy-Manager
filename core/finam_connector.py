import ctypes
import platform
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
import json

from loguru import logger

from core.base_connector import BaseConnector
from core.storage import get_setting
from core.moex_api import MOEXClient

CONNECTOR_ID = "finam"

# ── Тип callback-функции для DLL ─────────────────────────────────────────
_callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_char_p)


class FinamConnector(BaseConnector):
    """
    Коннектор Финам через txmlconnector64.dll (ctypes, без COM).
    DLL ищется в корне проекта.
    """

    def __init__(self):
        super().__init__()
        self._connected = False
        self._dll = None
        self._lock = threading.Lock()
        self._initialized = False
        # Храним ссылку на callback чтобы GC не собрал
        self._callback_ref = None
        # MOEX API клиент
        self.moex_client = MOEXClient()
        # Кэш данных, приходящих через callback
        self._securities: list[dict] = []
        self._positions: list[dict] = []
        self._accounts: list[dict] = []
        self._last_server_status: Optional[str] = None
        self._processed_trades: dict = {}  # {tradeno: timestamp} для дедупликации
        self._processed_trades_lock = threading.Lock()  # Lock для защиты _processed_trades
        self._last_cleanup = time.time()  # Время последней очистки старых записей
        # Для get_history — ожидание свечей из callback
        self._candles_event = threading.Event()
        self._candles_buffer: list[dict] = []
        # Подписка на свечи с callback (для LiveEngine)
        self._candle_callbacks: dict[tuple[str, int], list[Callable]] = {}  # (seccode, period) → [cb]
        self._candle_callbacks_lock = threading.Lock()
        # Котировки (подписка)
        self._quotes: dict[tuple[str, str], dict] = {}       # (board, seccode) → {"bid": float, "offer": float}
        self._quotes_lock = threading.Lock()
        self._quote_subscribers: dict[tuple[str, str], int] = {}  # refcount
        # Статусы ордеров
        self._order_status: dict[str, dict] = {}          # transactionid → {status, balance, quantity, orderno}
        self._order_status_lock = threading.Lock()
        self._order_watchers: dict[str, list[Callable]] = {}  # transactionid → callbacks
        # Информация по инструментам (sec_info / sec_info_upd)
        self._sec_info: dict[str, dict] = {}  # seccode → {buy_deposit, sell_deposit, point_cost, ...}
        self._sec_info_lock = threading.Lock()
        self._sec_info_event = threading.Event()  # для синхронного ожидания get_securities_info
        # Лимиты клиента (clientlimits)
        self._client_limits: dict[str, dict] = {}  # client_id → {money_free, money_current, ...}
        self._client_limits_lock = threading.Lock()

    # ── Внутренние утилиты DLL ────────────────────────────────────────────

    def _load_dll(self):
        """Загружает DLL если ещё не загружена."""
        if self._dll is not None:
            return
        base_dir = Path(__file__).resolve().parent.parent
        if platform.machine().endswith("64"):
            dll_path = base_dir / "txmlconnector64.dll"
        else:
            dll_path = base_dir / "txmlconnector.dll"

        if not dll_path.exists():
            raise FileNotFoundError(
                f"DLL не найдена: {dll_path}\n"
                f"Скачай с сайта Финам и положи в корень проекта."
            )
        self._dll = ctypes.WinDLL(str(dll_path))

        # Явно задаём типы аргументов и возвращаемых значений
        # Initialize(pszLogPath: LPCSTR, nLogLevel: c_int) -> c_void_p (0 = OK, иначе указатель на ошибку)
        self._dll.Initialize.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self._dll.Initialize.restype = ctypes.c_void_p

        # UnInitialize() -> c_void_p
        self._dll.UnInitialize.argtypes = []
        self._dll.UnInitialize.restype = ctypes.c_void_p

        # SetCallback(pCallback) -> c_bool
        self._dll.SetCallback.argtypes = [_callback_type]
        self._dll.SetCallback.restype = ctypes.c_bool

        # SendCommand(pData: LPCSTR) -> c_void_p (указатель на результат)
        self._dll.SendCommand.argtypes = [ctypes.c_char_p]
        self._dll.SendCommand.restype = ctypes.c_void_p

        # FreeMemory(pData: c_void_p) -> c_bool
        self._dll.FreeMemory.argtypes = [ctypes.c_void_p]
        self._dll.FreeMemory.restype = ctypes.c_bool

    def _get_message(self, ptr) -> str:
        """Читает строку из нативной памяти и освобождает её."""
        if not ptr:
            return ""
        try:
            msg = ctypes.string_at(ptr)
            self._dll.FreeMemory(ptr)
            return msg.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"[Finam] _get_message error (ptr={ptr:#x}): {e}")
            try:
                self._dll.FreeMemory(ptr)
            except Exception:
                pass
            return ""

    def _send_command(self, xml_str: str) -> str:
        """Отправляет XML-команду в DLL, возвращает XML-ответ."""
        xml_bytes = xml_str.encode("utf-8") + b"\0"
        with self._lock:
            ptr = self._dll.SendCommand(xml_bytes)
        return self._get_message(ptr)

    def _parse_error(self, xml_str: str) -> Optional[str]:
        """Проверяет ответ на наличие ошибки. Возвращает текст ошибки или None."""
        if not xml_str or not xml_str.strip():
            return "Пустой ответ от DLL"
        try:
            root = ET.fromstring(xml_str)
            if root.tag == "error":
                return root.text or "Неизвестная ошибка"
            if root.tag == "result" and root.get("success") == "false":
                msg = root.findtext("message", "") or root.text or "Неизвестная ошибка"
                return msg
        except ET.ParseError:
            # DLL вернула plain text вместо XML — это ошибка
            return xml_str.strip()
        return None

    # ── Callback от DLL ───────────────────────────────────────────────────

    def _on_dll_callback(self, msg_bytes: bytes) -> bool:
        """Вызывается DLL при входящих сообщениях (из другого потока)."""
        try:
            xml_str = msg_bytes.decode("utf-8", errors="replace")
            root = ET.fromstring(xml_str)
            tag = root.tag

            if tag == "server_status":
                connected = root.get("connected", "")
                if connected == "true":
                    if not self._connected:
                        self._connected = True
                        logger.info("[Finam] ✅ Подключён к серверу")
                        self._fire(self._on_connect)
                        # Запрашиваем лимиты клиентов после подключения
                        threading.Thread(target=self._request_client_limits, daemon=True).start()
                elif connected == "false":
                    if self._connected:
                        self._connected = False
                        logger.info("[Finam] Соединение с сервером потеряно")
                        self._fire(self._on_disconnect)
                elif connected == "error":
                    err_text = root.text or "Ошибка соединения"
                    logger.error(f"[Finam] Ошибка сервера: {err_text}")
                    self._connected = False
                    self._fire(self._on_error, err_text)

            elif tag == "securities":
                self._parse_securities(root)

            elif tag == "positions":
                self._parse_positions(root)

            elif tag == "clients":
                self._parse_clients(root)

            elif tag == "client":
                self._parse_client(root)

            elif tag == "error":
                err_text = root.text or "Ошибка"
                logger.error(f"[Finam] DLL error: {err_text}")
                self._fire(self._on_error, err_text)

            elif tag == "trades":
                self._parse_trades(root)

            elif tag == "candles":
                self._on_candles(root)

            elif tag == "quotations":
                self._parse_quotations(root)

            elif tag == "orders":
                self._parse_orders(root)

            elif tag == "sec_info":
                self._parse_sec_info(root)

            elif tag == "sec_info_upd":
                self._parse_sec_info_upd(root)

            elif tag == "clientlimits":
                self._parse_client_limits(root)

            elif tag == "portfolio_mct":
                self._parse_portfolio_mct(root)

            # Тихо игнорируем потоковые данные (стаканы и т.д.)
            elif tag in (
                "quotes", "alltrades", "ticks",
                "pits", "boards",
                "markets", "candlekinds",
                "messages", "news_header", "mc_portfolio",
                "portfolio_tplus", "united_portfolio",
                "overnight",
            ):
                pass

            else:
                logger.debug(f"[Finam] Неизвестный тег: {tag}")

        except Exception as e:
            logger.error(f"[Finam] Ошибка в callback: {e}")
        return True

    def _parse_securities(self, root):
        """Парсит список бумаг из callback.
        Сохраняем minstep и point_cost из структуры securities (раздел 4.6 TRANSAQ),
        чтобы они были доступны в get_sec_info до прихода sec_info callback.
        """
        result = []
        for sec in root.findall("security"):
            entry = {
                "ticker": sec.findtext("seccode", ""),
                "name": sec.findtext("shortname", ""),
                "board": sec.findtext("board", ""),
                "market": sec.findtext("market", ""),
            }
            for f in ("minstep", "point_cost", "lotsize"):
                v = sec.findtext(f)
                if v is not None:
                    try:
                        entry[f] = float(v)
                    except ValueError:
                        pass
            result.append(entry)
        if result:
            self._securities.extend(result)
            logger.debug(f"[Finam] Получено {len(result)} инструментов, всего в кэше: {len(self._securities)}")
            # Обновляем кэш sec_info minstep/point_cost если запись уже есть
            with self._sec_info_lock:
                for entry in result:
                    ticker = entry["ticker"]
                    if ticker in self._sec_info:
                        for f in ("minstep", "point_cost", "lotsize"):
                            if f in entry and f not in self._sec_info[ticker]:
                                self._sec_info[ticker][f] = entry[f]
    def _parse_positions(self, root):
        """Парсит позиции из callback (акции + фьючерсы)."""
        result = []
        # Акции / облигации (ММВБ)
        for pos in root.findall(".//sec_position"):
            ticker = pos.findtext("seccode", "")
            balance = float(pos.findtext("balance", "0") or "0")
            if balance == 0:
                continue
            result.append({
                "ticker": ticker,
                "board": pos.findtext("board", "TQBR"),
                "quantity": balance,
                "side": "buy" if balance > 0 else "sell",
                "avg_price": float(pos.findtext("open_balance", "0") or "0"),
                "current_price": 0.0,
                "pnl": 0.0,
            })
        # Фьючерсы (FORTS)
        for pos in root.findall(".//forts_position"):
            ticker = pos.findtext("seccode", "")
            total = int(float(pos.findtext("totalnet", "0") or "0"))
            if total == 0:
                continue
            openavgprice = float(pos.findtext("openavgprice", "0") or "0")
            result.append({
                "ticker": ticker,
                "board": "FUT",
                "quantity": total,
                "side": "buy" if total > 0 else "sell",
                "avg_price": openavgprice,
                "current_price": 0.0,
                "pnl": float(pos.findtext("varmargin", "0") or "0"),
            })
        self._positions = result
        self._fire(self._on_positions_update)

        # forts_money — свободные средства FORTS
        for fm in root.findall(".//forts_money"):
            client = fm.findtext("client", "")
            free = fm.findtext("free")
            current = fm.findtext("current")
            if client and free is not None:
                try:
                    limits = {"money_free": float(free)}
                    if current is not None:
                        limits["money_current"] = float(current)
                    with self._client_limits_lock:
                        existing = self._client_limits.get(client, {})
                        existing.update(limits)
                        self._client_limits[client] = existing
                    logger.debug(f"[Finam] forts_money [{client}]: money_free={limits['money_free']}")
                except ValueError:
                    pass

    def _parse_client(self, root):
        """Парсит одиночный тег <client> из callback."""
        cid = root.get("id", "")
        remove = root.get("remove", "false") == "true"
        if not cid:
            return
        union = root.findtext("union", "")
        market = root.findtext("market", "")
        ctype = root.findtext("type", "")
        currency = root.findtext("currency", "")

        if remove:
            self._accounts = [a for a in self._accounts if a["id"] != union]
            return

        forts_acc = root.findtext("forts_acc", "")

        # Сохраняем субсчёт для маршрутизации ордеров по рынкам
        sub = {"client_id": cid, "market": market, "type": ctype, "currency": currency, "forts_acc": forts_acc}
        if not union:
            return

        # Ищем существующий юнион
        existing = next((a for a in self._accounts if a["id"] == union), None)
        if existing:
            # Добавляем субсчёт если ещё нет
            subs = existing.setdefault("sub_accounts", [])
            if not any(s["client_id"] == cid for s in subs):
                subs.append(sub)
        else:
            self._accounts.append({
                "id": union,
                "name": union,
                "sub_accounts": [sub],
            })
            pass

    def _parse_trades(self, root):
        """Парсит исполненные сделки из callback и сохраняет в order_history."""
        from core.order_history import make_order, save_order, get_orders

        for trade in root.findall("trade"):
            tradeno = trade.findtext("tradeno", "")
            if not tradeno:
                continue
            
            # Проверяем и добавляем в _processed_trades под lock для избежания race condition
            with self._processed_trades_lock:
                if tradeno in self._processed_trades:
                    continue
                self._processed_trades[tradeno] = time.time()
            
            # Периодическая очистка (раз в час)
            if time.time() - self._last_cleanup > 3600:
                self._cleanup_old_trades()

            seccode = trade.findtext("seccode", "")
            buysell = trade.findtext("buysell", "")
            quantity = int(trade.findtext("quantity", "0") or "0")
            price = float(trade.findtext("price", "0") or "0")
            board = trade.findtext("board", "")
            brokerref = trade.findtext("brokerref", "")
            time_str = trade.findtext("time", "")

            if not seccode or not buysell or quantity <= 0:
                continue

            side = "buy" if buysell == "B" else "sell"
            strategy_id = brokerref  # agent_name передаётся как brokerref

            if not strategy_id:
                continue

            # Дедупликация: проверяем нет ли уже ордера с этим tradeno в истории
            existing = get_orders(strategy_id)
            if any(f"tradeno={tradeno}" in o.get("comment", "") for o in existing):
                continue

            # Парсим реальное время сделки из DLL
            trade_timestamp = None
            if time_str:
                try:
                    trade_timestamp = datetime.strptime(time_str, "%d.%m.%Y %H:%M:%S.%f").isoformat()
                except (ValueError, TypeError):
                    try:
                        trade_timestamp = datetime.strptime(time_str, "%d.%m.%Y %H:%M:%S").isoformat()
                    except (ValueError, TypeError):
                        pass

            order = make_order(
                strategy_id=strategy_id,
                ticker=seccode,
                side=side,
                quantity=quantity,
                price=price,
                board=board,
                comment=f"tradeno={tradeno} time={time_str}",
            )
            # Подменяем timestamp на реальное время сделки
            if trade_timestamp:
                order["timestamp"] = trade_timestamp
            save_order(order)
            logger.debug(
                f"[Finam] Сделка записана: {side.upper()} {seccode} "
                f"x{quantity} @ {price} [{strategy_id}] tradeno={tradeno}"
            )

    def _cleanup_old_trades(self):
        """Удаляет записи старше 24 часов"""
        cutoff = time.time() - 86400  # 24 часа
        with self._processed_trades_lock:
            to_remove = [tradeno for tradeno, ts in self._processed_trades.items() if ts < cutoff]
            for tradeno in to_remove:
                del self._processed_trades[tradeno]
        self._last_cleanup = time.time()
        logger.debug(f"[Finam] Очищено {len(to_remove)} старых записей сделок")

    def _parse_clients(self, root):
        """Парсит список клиентов/счетов из callback (обёртка <clients>)."""
        for client in root.findall("client"):
            self._parse_client(client)

    # ── Информация по инструментам (sec_info / sec_info_upd) ───────────

    # Поля, которые парсим из sec_info
    _SEC_INFO_FLOAT_FIELDS = (
        "clearing_price", "minprice", "maxprice",
        "buy_deposit", "sell_deposit", "bgo_c", "bgo_nc", "bgo_buy",
        "point_cost", "minstep", "accruedint", "coupon_value", "facevalue",
        "buybackprice",
    )
    _SEC_INFO_INT_FIELDS = ("coupon_period", "lot_volume")
    _SEC_INFO_STR_FIELDS = (
        "secname", "seccode", "pname", "mat_date", "coupon_date",
        "put_call", "opt_type", "isin", "regnumber", "buybackdate",
        "currencyid",
    )

    def _parse_sec_info(self, root):
        """Парсит <sec_info secid="..."> из callback."""
        seccode = root.findtext("seccode", "")
        if not seccode:
            return
        info: dict = {"secid": root.get("secid", ""), "market": root.findtext("market", "")}
        for f in self._SEC_INFO_STR_FIELDS:
            v = root.findtext(f)
            if v is not None:
                info[f] = v
        for f in self._SEC_INFO_FLOAT_FIELDS:
            v = root.findtext(f)
            if v is not None:
                try:
                    info[f] = float(v)
                except ValueError:
                    pass
        for f in self._SEC_INFO_INT_FIELDS:
            v = root.findtext(f)
            if v is not None:
                try:
                    info[f] = int(v)
                except ValueError:
                    pass
        with self._sec_info_lock:
            self._sec_info[seccode] = info
        self._sec_info_event.set()

    def _parse_sec_info_upd(self, root):
        """Парсит <sec_info_upd> — инкрементальное обновление.
        Финам НЕ присылает minstep в этом callback (только point_cost, ГО, лимиты цены).
        minstep берётся из структуры securities (4.6), которая приходит при подключении
        и сохраняется в _parse_securities → _sec_info через merge.
        """
        seccode = root.findtext("seccode", "")
        if not seccode:
            return
        with self._sec_info_lock:
            info = self._sec_info.get(seccode, {})
            for f in ("minprice", "maxprice", "buy_deposit", "sell_deposit",
                       "bgo_c", "bgo_nc", "bgo_buy", "point_cost"):
                v = root.findtext(f)
                if v is not None:
                    try:
                        info[f] = float(v)
                    except ValueError:
                        pass
            self._sec_info[seccode] = info

    def get_sec_info(self, ticker: str, board: str = "TQBR") -> Optional[dict]:
        """Возвращает информацию по инструменту с MOEX-валидацией.

        Приоритет: MOEX API > TRANSAQ DLL для point_cost, minstep, lotsize.
        TRANSAQ DLL часто возвращает некорректные данные (особенно point_cost и lotsize),
        поэтому MOEX данные используются как основной источник.
        """
        # 1. Получаем данные из TRANSAQ (существующая логика)
        with self._sec_info_lock:
            cached = self._sec_info.get(ticker)
            if cached:
                result = dict(cached)
            else:
                result = None

        if result is None:
            if not self._connected:
                return None

            # Запрашиваем у DLL
            market = self._BOARD_TO_MARKET.get(board, "1")
            cmd = (
                f'<command id="get_securities_info">'
                f'<security><market>{market}</market><seccode>{ticker}</seccode></security>'
                f'</command>'
            )
            self._sec_info_event.clear()
            response = self._send_command(cmd)
            err = self._parse_error(response)
            if err:
                logger.warning(f"[Finam] get_sec_info error: {err}")
                return None

            # Ждём callback sec_info
            if not self._sec_info_event.wait(timeout=5):
                logger.warning(f"[Finam] get_sec_info: таймаут {ticker}")
                return None

            with self._sec_info_lock:
                cached = self._sec_info.get(ticker)
                result = dict(cached) if cached else None

        if result is None:
            return None

        # 2. MOEX-валидация — предпочитаем MOEX для point_cost, minstep, lotsize
        try:
            # Определяем тип инструмента по board
            _FUT_BOARDS = {"FUT", "SPBFUT", "OPT"}
            sec_type = "futures" if board in _FUT_BOARDS else "stock"
            moex_info = self.moex_client.get_instrument_info(ticker, sec_type)

            if moex_info:
                # Для фьючерсов: валидируем и перезаписываем point_cost, minstep, lotsize
                # Для акций: валидируем и перезаписываем только minstep и lotsize
                # (у акций point_cost из MOEX = minstep, что не то же самое
                #  что point_cost из TRANSAQ = 1.0 — "1 рубль стоит 1 рубль")
                if sec_type == "futures":
                    _FIELD_MAP = [
                        ("point_cost", "point_cost"),
                        ("minstep", "minstep"),
                        ("lotsize", "lot_size"),
                    ]
                else:
                    _FIELD_MAP = [
                        ("minstep", "minstep"),
                        ("lotsize", "lot_size"),
                    ]

                for transaq_field, moex_field in _FIELD_MAP:
                    transaq_val = result.get(transaq_field)
                    moex_val = moex_info.get(moex_field)
                    if transaq_val and moex_val:
                        try:
                            t_val = float(transaq_val)
                            m_val = float(moex_val)
                            if t_val > 0 and abs(t_val - m_val) / max(t_val, 0.001) > 0.05:
                                logger.debug(
                                    f"[Finam] РАСХОЖДЕНИЕ {ticker}.{transaq_field}: "
                                    f"TRANSAQ={t_val}, MOEX={m_val} — используем MOEX"
                                )
                        except (ValueError, TypeError):
                            pass

                # Перезаписываем значениями от MOEX (если есть)
                if sec_type == "futures" and moex_info.get("point_cost"):
                    result["point_cost"] = moex_info["point_cost"]
                if moex_info.get("minstep"):
                    result["minstep"] = moex_info["minstep"]
                if moex_info.get("lot_size"):
                    result["lotsize"] = moex_info["lot_size"]

        except Exception as e:
            logger.debug(f"[Finam] MOEX-валидация {ticker} недоступна: {e}, используем TRANSAQ")

        return result

    def get_moex_info(self, ticker: str, sec_type: str = 'futures') -> Optional[dict]:
        """
        Получает параметры инструмента с MOEX API через MOEXClient.
        
        Args:
            ticker: Тикер инструмента (например, 'SiH5', 'SBER')
            sec_type: Тип инструмента ('futures' или 'stock')
            
        Returns:
            Словарь с параметрами:
            {
                'minstep': float,      # Минимальный шаг цены
                'point_cost': float,   # Стоимость пункта
                'lot_size': int,       # Количество в лоте
                'sec_type': str        # Тип инструмента
            }
            или None при ошибке
        """
        logger.debug(f"[Finam] Запрос MOEX info для {ticker}, тип: {sec_type}")
        
        try:
            result = self.moex_client.get_instrument_info(ticker, sec_type)
            
            if result:
                logger.info(f"[Finam] MOEX info для {ticker}: point_cost={result.get('point_cost')}, "
                           f"minstep={result.get('minstep')}, lot_size={result.get('lot_size')}")
            else:
                logger.warning(f"[Finam] Не удалось получить MOEX info для {ticker}")
            
            return result
            
        except Exception as e:
            logger.error(f"[Finam] Ошибка при получении MOEX info для {ticker}: {e}", exc_info=True)
            return None

    # ── Лимиты клиента (clientlimits) ────────────────────────────────────

    _CLIENT_LIMITS_FIELDS = (
        "cbplimit", "cbplused", "cbplplanned",
        "fob_varmargin", "coverage", "liquidity_c", "profit",
        "money_current", "money_reserve", "money_free",
        "options_premium", "exchange_fee",
        "forts_varmargin", "varmargin", "pclmargin", "options_vm",
        "spot_buy_limit", "used_stop_buy_limit",
        "collat_current", "collat_blocked", "collat_free",
    )

    def _parse_client_limits(self, root):
        """Парсит <clientlimits client='...'> из callback."""
        client = root.get("client", "")
        if not client:
            return
        limits = {}
        for f in self._CLIENT_LIMITS_FIELDS:
            v = root.findtext(f)
            if v is not None:
                try:
                    limits[f] = float(v)
                except ValueError:
                    pass
        with self._client_limits_lock:
            self._client_limits[client] = limits
        logger.info(f"[Finam] clientlimits [{client}]: money_free={limits.get('money_free')}")

    def _request_client_limits(self):
        """Запрашивает лимиты клиентов через get_mc_portfolio после подключения."""
        import time
        time.sleep(2)  # Ждём пока придут clients/accounts
        try:
            with self._client_limits_lock:
                clients = list(self._client_limits.keys())

            # Берём client_id из sub_accounts
            all_clients = set()
            for acc in self._accounts:
                for sub in acc.get("sub_accounts", []):
                    cid = sub.get("client_id")
                    if cid:
                        all_clients.add(cid)

            for cid in all_clients:
                cmd = f'<command id="get_portfolio_mct" client="{cid}"/>'
                self._send_command(cmd)
                logger.debug(f"[Finam] Запрошен get_portfolio_mct для {cid}")
        except Exception as e:
            logger.warning(f"[Finam] _request_client_limits error: {e}")

    def _parse_portfolio_mct(self, root):
        """Парсит <portfolio_mct client='...'> — свободные средства из coverage_fact."""
        client = root.get("client", "")
        if not client:
            return
        limits = {}
        for field in ("capital", "coverage_fact", "coverage_plan", "open_balance",
                      "pnl_income", "pnl_intraday"):
            v = root.findtext(field)
            if v is not None:
                try:
                    limits[field] = float(v)
                except ValueError:
                    pass
        if "capital" in limits:
            limits["money_free"] = limits["capital"]
        with self._client_limits_lock:
            existing = self._client_limits.get(client, {})
            existing.update(limits)
            self._client_limits[client] = existing
        logger.debug(f"[Finam] portfolio_mct [{client}]: {limits}")

    def get_client_limits(self, client_id: str) -> Optional[dict]:
        """Возвращает кэшированные лимиты клиента."""
        with self._client_limits_lock:
            cached = self._client_limits.get(client_id)
            return dict(cached) if cached else None

    def get_free_money(self, account_id: str) -> Optional[float]:
        """Свободные средства на счёте (money_free из clientlimits/forts_money)."""
        with self._client_limits_lock:
            # Точное совпадение по client_id
            if account_id in self._client_limits:
                return self._client_limits[account_id].get("money_free")
            # Поиск по forts_acc: ищем client у которого forts_acc == account_id
            for acc in self._accounts:
                for sub in acc.get("sub_accounts", []):
                    if sub.get("forts_acc") == account_id:
                        cid = sub["client_id"]
                        if cid in self._client_limits:
                            return self._client_limits[cid].get("money_free")
            # Поиск по подстроке
            for cid, limits in self._client_limits.items():
                if account_id in cid or cid in account_id:
                    return limits.get("money_free")
            # Fallback: первый ненулевой money_free
            for limits in self._client_limits.values():
                v = limits.get("money_free")
                if v is not None and v != 0.0:
                    return v
        return None

    def subscribe_quotes(self, board: str, seccode: str):
        """Подписаться на котировки инструмента (refcount)."""
        key = (board, seccode)
        with self._quotes_lock:
            cnt = self._quote_subscribers.get(key, 0)
            self._quote_subscribers[key] = cnt + 1
            if cnt > 0:
                return  # уже подписаны
        cmd = (
            f'<command id="subscribe">'
            f'<quotations><security><board>{board}</board>'
            f'<seccode>{seccode}</seccode></security></quotations>'
            f'</command>'
        )
        resp = self._send_command(cmd)
        err = self._parse_error(resp)
        if err:
            logger.warning(f"[Finam] subscribe_quotes {seccode}: {err}")

    def unsubscribe_quotes(self, board: str, seccode: str):
        """Отписаться от котировок (refcount). При 0 — реальная отписка."""
        key = (board, seccode)
        with self._quotes_lock:
            cnt = self._quote_subscribers.get(key, 0)
            if cnt <= 1:
                self._quote_subscribers.pop(key, None)
            else:
                self._quote_subscribers[key] = cnt - 1
                return  # ещё есть подписчики
        cmd = (
            f'<command id="unsubscribe">'
            f'<quotations><security><board>{board}</board>'
            f'<seccode>{seccode}</seccode></security></quotations>'
            f'</command>'
        )
        resp = self._send_command(cmd)
        err = self._parse_error(resp)
        if err:
            logger.warning(f"[Finam] unsubscribe_quotes {seccode}: {err}")

    def get_best_quote(self, board: str, seccode: str) -> Optional[dict]:
        """Возвращает {"bid": float, "offer": float} или None."""
        with self._quotes_lock:
            return self._quotes.get((board, seccode))

    def get_order_book(self, board: str, ticker: str, depth: int = 10) -> Optional[dict]:
        """
        Получить стакан заявок. Finam txmlconnector не предоставляет полный стакан,
        возвращаем заглушку с лучшими ценами из котировок.
        
        Returns:
            {"bids": [(price, volume), ...], "asks": [(price, volume), ...]}
            Для Finam возвращаем только первый уровень из get_best_quote
        """
        quote = self.get_best_quote(board, ticker)
        if not quote:
            return None
        
        bid = quote.get("bid", 0.0)
        offer = quote.get("offer", 0.0)
        
        # Возвращаем заглушку: только лучшие цены с условным объёмом 1
        # Это позволит проверке ликвидности работать, но всегда показывать warning
        bids = [(bid, 1.0)] if bid > 0 else []
        asks = [(offer, 1.0)] if offer > 0 else []
        
        return {"bids": bids, "asks": asks}

    def _parse_quotations(self, root):
        """Парсит <quotations> из callback, мержит инкрементально."""
        for q in root.findall("quotation"):
            board = q.get("board", "")
            seccode = q.get("seccode", "")
            if not board or not seccode:
                continue
            key = (board, seccode)
            with self._quotes_lock:
                cur = self._quotes.get(key, {})
                for field in ("bid", "offer", "last"):
                    val = q.findtext(field)
                    if val is not None:
                        try:
                            cur[field] = float(val)
                        except ValueError:
                            pass
                if cur:
                    self._quotes[key] = cur

    # ── Отслеживание статуса ордеров ─────────────────────────────────────

    def _parse_orders(self, root):
        """Парсит <orders> из callback, обновляет статусы и вызывает watchers."""
        for order in root.findall("order"):
            tid = order.get("transactionid", "")
            if not tid:
                continue
            status = order.findtext("status", "") or order.get("status", "")
            balance_str = order.findtext("balance", "")
            quantity_str = order.findtext("quantity", "")
            orderno = order.findtext("orderno", "") or order.get("orderno", "")

            info: dict = {"status": status}
            if orderno:
                info["orderno"] = orderno
            if balance_str:
                try:
                    info["balance"] = int(balance_str)
                except ValueError:
                    pass
            if quantity_str:
                try:
                    info["quantity"] = int(quantity_str)
                except ValueError:
                    pass

            with self._order_status_lock:
                self._order_status[tid] = info
                watchers = list(self._order_watchers.get(tid, []))

            for cb in watchers:
                try:
                    cb(tid, info)
                except Exception as e:
                    logger.warning(f"[Finam] order watcher error: {e}")

    def get_order_status(self, transaction_id: str) -> Optional[dict]:
        with self._order_status_lock:
            return self._order_status.get(transaction_id)

    def watch_order(self, transaction_id: str, callback: Callable):
        """Регистрирует callback(tid, info) на изменение статуса ордера."""
        with self._order_status_lock:
            self._order_watchers.setdefault(transaction_id, []).append(callback)

    def unwatch_order(self, transaction_id: str, callback: Callable):
        with self._order_status_lock:
            lst = self._order_watchers.get(transaction_id, [])
            try:
                lst.remove(callback)
            except ValueError:
                pass

    # ── Подключение ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        login = (get_setting("finam_login") or "").strip()
        password = (get_setting("finam_password") or "").strip()
        host = (get_setting("finam_host") or "tr1.finam.ru").strip()
        port = int(get_setting("finam_port") or 3900)

        if not login or not password:
            logger.warning("[Finam] Логин/пароль не настроены")
            self._fire(self._on_error, "Логин или пароль не указаны в настройках")
            return False

        logger.info(f"[Finam] Подключение → {host}:{port}")
        try:
            self._load_dll()

            # Очищаем кэши перед новым подключением
            self._securities.clear()
            self._accounts.clear()
            self._positions.clear()

            # Инициализация DLL (один раз)
            if not self._initialized:
                log_dir = str(Path(__file__).resolve().parent.parent / "logs" / "transaq")
                Path(log_dir).mkdir(parents=True, exist_ok=True)
                log_dir_bytes = (log_dir + "\\").encode("utf-8")
                err = self._dll.Initialize(log_dir_bytes, 2)
                if err:
                    msg = self._get_message(err)
                    if msg and "already initialized" not in msg.lower():
                        raise RuntimeError(f"Initialize failed: {msg}")
                    logger.warning(f"[Finam] Initialize: {msg or 'ненулевой код, продолжаем'}")

                # Устанавливаем callback
                self._callback_ref = _callback_type(self._on_dll_callback)
                if not self._dll.SetCallback(self._callback_ref):
                    raise RuntimeError("SetCallback failed")

                self._initialized = True

            # Отправляем команду connect
            from xml.sax.saxutils import escape as xml_escape
            cmd = (
                f'<command id="connect">'
                f'<login>{xml_escape(login)}</login>'
                f'<password>{xml_escape(password)}</password>'
                f'<host>{host}</host>'
                f'<port>{port}</port>'
                f'<language>ru</language>'
                f'<autopos>true</autopos>'
                f'<micex_registers>true</micex_registers>'
                f'<milliseconds>true</milliseconds>'
                f'<push_u_limits>60</push_u_limits>'
                f'<rqdelay>100</rqdelay>'
                f'<session_timeout>120</session_timeout>'
                f'<request_timeout>20</request_timeout>'
                f'</command>'
            )
            response = self._send_command(cmd)
            err = self._parse_error(response)
            if err:
                logger.error(f"[Finam] Ошибка подключения: {err}")
                self._fire(self._on_error, err)
                return False

            # Команда принята — реальный статус придёт через callback
            self._stop_reconnect.clear()
            self.start_reconnect_loop()  # Запускаем мониторинг реконнекта
            logger.info("[Finam] Команда connect отправлена, ожидаем ответ сервера...")
            return True

        except Exception as e:
            logger.error(f"[Finam] Exception: {e}")
            self._fire(self._on_error, str(e))
            return False

    def disconnect(self):
        self._stop_reconnect.set()
        try:
            if self._dll and self._initialized:
                cmd = '<command id="disconnect"/>'
                self._send_command(cmd)
        except Exception as e:
            logger.warning(f"[Finam] disconnect error: {e}")
        finally:
            self._connected = False
            logger.info("[Finam] Отключён")
            self._fire(self._on_disconnect)

    def is_connected(self) -> bool:
        return self._connected

    # ── Маппинг board → market для резолва субсчёта ─────────────────────
    _BOARD_TO_MARKET = {
        "FUT": "4",  "SPBFUT": "4",       # фьючерсы
        "OPT": "4",                         # опционы
        "TQBR": "1", "TQCB": "1",          # ММВБ акции/облигации
        "TQOB": "1", "TQDE": "1",
        "CETS": "2",                        # валюта
    }

    def _resolve_client_id(self, account_id: str, board: str) -> str:
        """Находит client_id субсчёта по union-счёту и борду."""
        market = self._BOARD_TO_MARKET.get(board, "1")
        account = next((a for a in self._accounts if a["id"] == account_id), None)
        if not account:
            logger.warning(f"[Finam] Счёт {account_id} не найден в кэше")
            return account_id
        subs = account.get("sub_accounts", [])
        for sub in subs:
            if sub.get("market") == market:
                return sub["client_id"]
        # Если не нашли по market — пробуем первый субсчёт
        if subs:
            logger.warning(f"[Finam] Не найден субсчёт для market={market}, используем {subs[0]['client_id']}")
            return subs[0]["client_id"]
        return account_id

    # ── Ордера ────────────────────────────────────────────────────────────

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
        if not self._connected:
            logger.warning("[Finam] place_order — нет подключения")
            return None
        try:
            client_id = self._resolve_client_id(account_id, board)
            buysell = "B" if side == "buy" else "S"
            cmd = f'<command id="neworder">'
            cmd += f'<security><board>{board}</board><seccode>{ticker}</seccode></security>'
            cmd += f'<client>{client_id}</client>'
            cmd += f'<buysell>{buysell}</buysell>'
            cmd += f'<quantity>{quantity}</quantity>'
            if order_type == "market":
                cmd += '<bymarket/>'
            else:
                cmd += f'<price>{price}</price>'
            if agent_name:
                cmd += f'<brokerref>{agent_name}</brokerref>'
            cmd += '</command>'

            response = self._send_command(cmd)
            err = self._parse_error(response)
            if err:
                logger.error(f"[Finam] Ордер отклонён: {err}")
                self._fire(self._on_error, err)
                return None

            # Парсим transactionid из ответа
            try:
                root = ET.fromstring(response)
                tid = root.get("transactionid", "")
            except ET.ParseError:
                tid = ""

            logger.info(f"[Finam] Ордер {side} {ticker}x{quantity} board={board}: tid={tid}")
            return tid or None
        except Exception as e:
            logger.error(f"[Finam] place_order error: {e}")
            self._fire(self._on_error, str(e))
            return None

    def cancel_order(self, order_id: str, account_id: str) -> bool:
        if not self._connected:
            return False
        try:
            cmd = (
                f'<command id="cancelorder">'
                f'<transactionid>{order_id}</transactionid>'
                f'</command>'
            )
            response = self._send_command(cmd)
            err = self._parse_error(response)
            if err:
                logger.error(f"[Finam] cancel_order: {err}")
                return False
            return True
        except Exception as e:
            logger.error(f"[Finam] cancel_order error: {e}")
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
        total_qty = int(abs(float(pos.get("quantity", 0))))
        close_qty = quantity if 0 < quantity < total_qty else total_qty
        side = "sell" if float(pos.get("quantity", 0)) > 0 else "buy"
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

    # ── Позиции / счета ───────────────────────────────────────────────────

    def get_positions(self, account_id: str) -> list[dict]:
        """Возвращает кэшированные позиции (обновляются через callback)."""
        return self._positions

    def get_all_positions(self) -> dict:
        """Возвращает позиции в формате {account_id: [positions]} для PositionManager."""
        if self._accounts and self._positions:
            return {acc["id"]: self._positions for acc in self._accounts}
        return {}

    def get_accounts(self) -> list[dict]:
        """Возвращает кэшированные счета (обновляются через callback)."""
        return self._accounts

    def get_securities(self, board: str = "") -> list[dict]:
        """Возвращает кэшированные бумаги, опционально фильтруя по борду."""
        if board:
            return [s for s in self._securities if s.get("board") == board]
        return self._securities

    def get_last_price(self, ticker: str, board: str = "TQBR") -> Optional[float]:
        if not self._connected:
            return None
        try:
            market = self._BOARD_TO_MARKET.get(board, "1")
            cmd = (
                f'<command id="get_securities_info">'
                f'<security><market>{market}</market><seccode>{ticker}</seccode></security>'
                f'</command>'
            )
            response = self._send_command(cmd)
            root = ET.fromstring(response)
            # Ответ придёт через callback, но попробуем синхронный парсинг
            price = root.findtext("last", None)
            return float(price) if price else None
        except Exception as e:
            logger.warning(f"[Finam] get_last_price error: {e}")
            return None

    # ── История свечей ────────────────────────────────────────────────────

    # TransAQ candlekind id → период (chart_window передаёт "1m", "5m" и т.д.)
    _PERIOD_TO_CANDLEKIND = {
        "1m": 1, "5m": 2, "15m": 3, "30m": 4,
        "1h": 5, "4h": 6, "1d": 7,
    }

    def subscribe_candles(self, board: str, seccode: str, period: int,
                          count: int, callback: Callable):
        """Подписка на свечи с регистрацией callback для LiveEngine.

        Args:
            board: борд инструмента (FUT, TQBR, ...)
            seccode: код инструмента
            period: candlekind id (1=1m, 2=5m, 3=15m, 4=30m, 5=1h)
            count: кол-во исторических свечей при подписке
            callback: функция(list[dict]) — вызывается при получении новых свечей
        """
        key = (seccode, period)
        with self._candle_callbacks_lock:
            cbs = self._candle_callbacks.setdefault(key, [])
            if callback not in cbs:
                cbs.append(callback)

        cmd = (
            f'<command id="subscribe">'
            f'<candles><security><board>{board}</board>'
            f'<seccode>{seccode}</seccode></security>'
            f'<period>{period}</period><count>{count}</count>'
            f'</candles></command>'
        )
        response = self._send_command(cmd)
        err = self._parse_error(response)
        if err:
            logger.warning(f"[Finam] subscribe_candles {seccode} period={period}: {err}")
        else:
            logger.info(f"[Finam] subscribe_candles {seccode} period={period}: OK, "
                        f"registered key={key}")

    def unsubscribe_candles(self, board: str, seccode: str, period: int,
                            callback: Callable):
        """Отписка callback от свечей. При 0 подписчиков — реальная отписка от DLL."""
        key = (seccode, period)
        with self._candle_callbacks_lock:
            cbs = self._candle_callbacks.get(key, [])
            try:
                cbs.remove(callback)
            except ValueError:
                pass
            remaining = len(cbs)
            if remaining == 0:
                self._candle_callbacks.pop(key, None)

        if remaining == 0:
            cmd = (
                f'<command id="unsubscribe">'
                f'<candles><security><board>{board}</board>'
                f'<seccode>{seccode}</seccode></security>'
                f'<period>{period}</period></candles></command>'
            )
            self._send_command(cmd)

    def _on_candles(self, root):
        """Callback: парсит <candles> и складывает в буфер + вызывает подписчиков."""
        status = root.get("status", "")
        seccode = root.get("seccode", "?")
        period = int(root.get("period", "0") or "0")
        rows = []
        for c in root.findall("candle"):
            try:
                rows.append({
                    "date":   c.get("date", ""),
                    "open":   float(c.get("open", 0)),
                    "high":   float(c.get("high", 0)),
                    "low":    float(c.get("low", 0)),
                    "close":  float(c.get("close", 0)),
                    "volume": int(c.get("volume", 0)),
                })
            except (ValueError, TypeError):
                continue

        # status=0: стриминговое обновление (новая/обновлённая свеча)
        # status=1: полный набор данных (ответ на subscribe или gethistorydata)
        # status=2: начало потока данных
        # status=3: нет данных

        if status == "3":
            logger.warning(f"[Finam] Сервер вернул status=3 (нет данных) для {seccode}")
            self._candles_buffer = []
            self._candles_event.set()
            return

        # Для get_history (синхронный запрос) — всегда обновляем буфер
        self._candles_buffer = rows
        self._candles_event.set()

        # Вызываем зарегистрированные callbacks для (seccode, period)
        if rows:
            key = (seccode, period)
            with self._candle_callbacks_lock:
                cbs = list(self._candle_callbacks.get(key, []))
            if cbs:
                for cb in cbs:
                    try:
                        cb(rows, status)
                    except Exception as e:
                        logger.error(f"[Finam] candle callback error ({seccode}): {e}")

    def get_history(self, ticker: str, board: str,
                    period: str, days: int):
        """Запрашивает свечи через DLL и возвращает DataFrame."""
        if not self._connected:
            return None
        try:
            import pandas as pd
            from datetime import datetime, timedelta

            kind = self._PERIOD_TO_CANDLEKIND.get(period, 3)
            # Кол-во баров по таймфрейму: торговых минут в дне ~840 (фьючерсы)
            bars_per_day = {
                "1m": 840, "5m": 168, "15m": 56, "30m": 28,
                "1h": 14, "4h": 4, "1d": 1,
            }
            bpd = bars_per_day.get(period, 56)
            count = days * bpd

            # Пробуем gethistorydata, при ошибке/таймауте — fallback на subscribe
            self._candles_event.clear()
            self._candles_buffer = []

            cmd = (
                f'<command id="gethistorydata">'
                f'<security><board>{board}</board><seccode>{ticker}</seccode></security>'
                f'<period>{kind}</period>'
                f'<count>{count}</count>'
                f'<reset>true</reset>'
                f'</command>'
            )
            response = self._send_command(cmd)
            err = self._parse_error(response)
            if err:
                logger.warning(f"[Finam] get_history error: {err}, пробуем subscribe")
                return self._get_history_via_subscribe(ticker, board, kind, days)

            if not self._candles_event.wait(timeout=10):
                logger.warning(f"[Finam] get_history: таймаут {ticker}, пробуем subscribe")
                return self._get_history_via_subscribe(ticker, board, kind, days)

            rows = self._candles_buffer
            if not rows:
                return None

            for r in rows:
                date_str = r.pop("date")
                # DLL может прислать дату с миллисекундами или без
                try:
                    r["datetime"] = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S.%f")
                except ValueError:
                    r["datetime"] = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")

            df = pd.DataFrame(rows)
            df.rename(columns={
                "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            }, inplace=True)
            df.set_index("datetime", inplace=True)
            df.sort_index(inplace=True)

            cutoff = datetime.now() - timedelta(days=days)
            df = df[df.index >= cutoff]

            logger.debug(f"[Finam] get_history {ticker} {period}: {len(df)} свечей")
            return df if not df.empty else None

        except Exception as e:
            logger.error(f"[Finam] get_history error: {e}")
            return None

    def _get_history_via_subscribe(self, ticker: str, board: str,
                                    kind: int, days: int):
        """Fallback: получает свечи через subscribe вместо gethistorydata."""
        import pandas as pd
        from datetime import datetime, timedelta

        self._candles_event.clear()
        self._candles_buffer = []

        cmd = (
            f'<command id="subscribe">'
            f'<candles><security><board>{board}</board>'
            f'<seccode>{ticker}</seccode></security>'
            f'<period>{kind}</period><count>{days * 500}</count>'
            f'</candles></command>'
        )
        response = self._send_command(cmd)
        err = self._parse_error(response)
        if err:
            logger.warning(f"[Finam] subscribe candles error: {err}")
            return None

        if not self._candles_event.wait(timeout=15):
            logger.warning(f"[Finam] subscribe candles: таймаут {ticker}")
            # Отписываемся
            unsub = (
                f'<command id="unsubscribe">'
                f'<candles><security><board>{board}</board>'
                f'<seccode>{ticker}</seccode></security>'
                f'<period>{kind}</period></candles></command>'
            )
            self._send_command(unsub)
            return None

        # Отписываемся после получения данных
        unsub = (
            f'<command id="unsubscribe">'
            f'<candles><security><board>{board}</board>'
            f'<seccode>{ticker}</seccode></security>'
            f'<period>{kind}</period></candles></command>'
        )
        self._send_command(unsub)

        rows = self._candles_buffer
        if not rows:
            return None

        for r in rows:
            date_str = r.pop("date")
            try:
                r["datetime"] = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S.%f")
            except ValueError:
                r["datetime"] = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")

        df = pd.DataFrame(rows)
        df.rename(columns={
            "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }, inplace=True)
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)

        cutoff = datetime.now() - timedelta(days=days)
        df = df[df.index >= cutoff]

        logger.info(f"[Finam] _get_history_via_subscribe {ticker}: {len(df)} свечей")
        return df if not df.empty else None

    # ── Chase Order ─────────────────────────────────────────────────────

    def chase_order(self, account_id: str, ticker: str, side: str, quantity: int,
                    board: str = "TQBR", agent_name: str = ""):
        """Создаёт ChaseOrder — лимитка по лучшей цене с автоперестановкой."""
        from core.chase_order import ChaseOrder
        return ChaseOrder(
            connector=self,
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            board=board,
            agent_name=agent_name,
        )

    # ── Деинициализация ───────────────────────────────────────────────────

    def shutdown(self):
        """Полная деинициализация DLL. Вызывать при завершении приложения."""
        if self._connected:
            self.disconnect()
        if self._dll and self._initialized:
            try:
                err = self._dll.UnInitialize()
                if err != 0:
                    msg = self._get_message(err)
                    logger.warning(f"[Finam] UnInitialize: {msg}")
                else:
                    logger.info("[Finam] DLL деинициализирована")
            except Exception as e:
                logger.warning(f"[Finam] UnInitialize error: {e}")
            self._initialized = False


finam_connector = FinamConnector()
