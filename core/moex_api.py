"""
Модуль для работы с MOEX ISS API.

Предоставляет универсальный интерфейс для получения параметров инструментов
с Московской биржи (фьючерсы, акции).
"""

import logging
import time as _time
import requests
from typing import Optional, Dict, Any
from threading import Lock


logger = logging.getLogger(__name__)


class MOEXClient:
    """
    Клиент для работы с MOEX ISS API.
    
    Поддерживает получение параметров инструментов:
    - Фьючерсы (FORTS)
    - Акции (Stock market)
    
    Использует кэширование для минимизации запросов к API.
    """
    
    # Кэш для хранения данных инструментов: key → (data, monotonic_timestamp)
    _cache: Dict[str, tuple[Dict[str, Any], float]] = {}
    _cache_lock = Lock()

    # TTL для разных типов инструментов (секунды)
    _CACHE_TTL_FUTURES = 4 * 3600   # 4 часа (STEPPRICE меняется при клиринге)
    _CACHE_TTL_STOCKS = 24 * 3600   # 24 часа (minstep/lotsize стабильны)
    
    # Таймаут для HTTP запросов (секунды)
    REQUEST_TIMEOUT = 10
    
    # User-Agent для запросов
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    
    @classmethod
    def get_futures_info(cls, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Получает параметры фьючерса с MOEX API.
        
        Args:
            ticker: Тикер фьючерса (например, 'SiH5', 'RIH5')
            
        Returns:
            Словарь с параметрами:
            {
                'minstep': float,      # Минимальный шаг цены
                'point_cost': float,   # Стоимость пункта (STEPPRICE / MINSTEP)
                'lot_size': int,       # Количество в лоте
                'sec_type': str        # 'futures'
            }
            или None при ошибке
        """
        # Проверяем кэш
        cache_key = f"futures:{ticker.upper()}"
        with cls._cache_lock:
            if cache_key in cls._cache:
                data, cached_at = cls._cache[cache_key]
                if _time.monotonic() - cached_at < cls._CACHE_TTL_FUTURES:
                    logger.debug(f"[MOEX] Returning cached data for {ticker}")
                    return data
                else:
                    del cls._cache[cache_key]  # TTL истёк
        
        try:
            # Формируем URL для запроса
            url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{ticker}.json"
            params = {
                'iss.meta': 'off',
                'iss.only': 'securities',
                'securities.columns': 'SECID,MINSTEP,STEPPRICE,LOTVOLUME'
            }
            
            # Выполняем запрос
            headers = {'User-Agent': cls.USER_AGENT}
            response = requests.get(url, params=params, headers=headers, timeout=cls.REQUEST_TIMEOUT)
            response.raise_for_status()
            
            # Парсим JSON
            data = response.json()
            securities = data.get('securities', {}).get('data', [])
            
            if not securities:
                logger.warning(f"[MOEX] No data found for futures {ticker}")
                return None
            
            # Ищем нужный тикер в данных
            for row in securities:
                if len(row) >= 4 and row[0].upper() == ticker.upper():
                    minstep = row[1]      # MINSTEP
                    stepprice = row[2]    # STEPPRICE
                    lotvolume = row[3]    # LOTVOLUME
                    
                    # Проверяем наличие всех необходимых данных
                    if minstep is None or stepprice is None:
                        logger.warning(f"[MOEX] Incomplete data for futures {ticker}: MINSTEP={minstep}, STEPPRICE={stepprice}")
                        return None
                    
                    # Вычисляем point_cost
                    point_cost = float(stepprice) / float(minstep)
                    lot_size = int(lotvolume) if lotvolume else 1
                    
                    result = {
                        'minstep': float(minstep),
                        'point_cost': point_cost,
                        'lot_size': lot_size,
                        'sec_type': 'futures'
                    }
                    
                    # Сохраняем в кэш с timestamp
                    with cls._cache_lock:
                        cls._cache[cache_key] = (result, _time.monotonic())

                    logger.info(f"[MOEX] Futures {ticker}: MINSTEP={minstep}, STEPPRICE={stepprice}, "
                               f"point_cost={point_cost}, lot_size={lot_size}")
                    return result

            logger.warning(f"[MOEX] Ticker {ticker} not found in futures data")
            return None
            
        except requests.exceptions.HTTPError as e:
            logger.warning(f"[MOEX] HTTP error for futures {ticker}: {e.response.status_code} {e.response.reason}")
            return None
        except requests.exceptions.Timeout:
            logger.warning(f"[MOEX] Request timeout for futures {ticker}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"[MOEX] Request error for futures {ticker}: {e}")
            return None
        except (ValueError, KeyError, IndexError) as e:
            logger.warning(f"[MOEX] Data parsing error for futures {ticker}: {e}")
            return None
        except Exception as e:
            logger.error(f"[MOEX] Unexpected error for futures {ticker}: {e}", exc_info=True)
            return None
    
    @classmethod
    def get_stock_info(cls, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Получает параметры акции с MOEX API.
        
        Args:
            ticker: Тикер акции (например, 'SBER', 'GAZP')
            
        Returns:
            Словарь с параметрами:
            {
                'minstep': float,      # Минимальный шаг цены
                'point_cost': float,   # Стоимость пункта (для акций = minstep)
                'lot_size': int,       # Количество в лоте
                'sec_type': str        # 'stock'
            }
            или None при ошибке
        """
        # Проверяем кэш
        cache_key = f"stock:{ticker.upper()}"
        with cls._cache_lock:
            if cache_key in cls._cache:
                data, cached_at = cls._cache[cache_key]
                if _time.monotonic() - cached_at < cls._CACHE_TTL_STOCKS:
                    logger.debug(f"[MOEX] Returning cached data for {ticker}")
                    return data
                else:
                    del cls._cache[cache_key]  # TTL истёк
        
        try:
            # Формируем URL для запроса — добавляем BOARDID для фильтрации
            url = f"https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}.json"
            params = {
                'iss.meta': 'off',
                'iss.only': 'securities',
                'securities.columns': 'SECID,BOARDID,MINSTEP,LOTSIZE'
            }

            # Выполняем запрос
            headers = {'User-Agent': cls.USER_AGENT}
            response = requests.get(url, params=params, headers=headers, timeout=cls.REQUEST_TIMEOUT)
            response.raise_for_status()

            # Парсим JSON
            data = response.json()
            securities = data.get('securities', {}).get('data', [])

            if not securities:
                logger.warning(f"[MOEX] No data found for stock {ticker}")
                return None

            # Ищем нужный тикер — предпочитаем основные борды (TQBR, TQTF, TQOB)
            # MOEX может вернуть несколько строк для разных борд (TQBR, SPEQ и т.д.)
            _PREFERRED_BOARDS = ("TQBR", "TQTF", "TQOB", "TQIF")
            best_row = None
            fallback_row = None

            for row in securities:
                if len(row) >= 4 and row[0].upper() == ticker.upper():
                    board_id = row[1]  # BOARDID
                    if board_id in _PREFERRED_BOARDS:
                        best_row = row
                        break  # нашли основную борду — берём сразу
                    elif fallback_row is None:
                        fallback_row = row

            chosen_row = best_row or fallback_row
            if chosen_row is None:
                logger.warning(f"[MOEX] Ticker {ticker} not found in stock data")
                return None

            board_id = chosen_row[1]
            minstep = chosen_row[2]    # MINSTEP
            lotsize = chosen_row[3]    # LOTSIZE

            # Проверяем наличие необходимых данных
            if minstep is None:
                logger.warning(f"[MOEX] Incomplete data for stock {ticker}: MINSTEP={minstep}")
                return None

            # Для акций point_cost = minstep (стоимость минимального шага)
            minstep_float = float(minstep)
            lot_size = int(lotsize) if lotsize else 1

            result = {
                'minstep': minstep_float,
                'point_cost': minstep_float,
                'lot_size': lot_size,
                'sec_type': 'stock'
            }

            # Сохраняем в кэш с timestamp
            with cls._cache_lock:
                cls._cache[cache_key] = (result, _time.monotonic())

            logger.info(f"[MOEX] Stock {ticker} (board={board_id}): MINSTEP={minstep}, "
                       f"point_cost={minstep_float}, lot_size={lot_size}")
            return result
            
        except requests.exceptions.HTTPError as e:
            logger.warning(f"[MOEX] HTTP error for stock {ticker}: {e.response.status_code} {e.response.reason}")
            return None
        except requests.exceptions.Timeout:
            logger.warning(f"[MOEX] Request timeout for stock {ticker}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"[MOEX] Request error for stock {ticker}: {e}")
            return None
        except (ValueError, KeyError, IndexError) as e:
            logger.warning(f"[MOEX] Data parsing error for stock {ticker}: {e}")
            return None
        except Exception as e:
            logger.error(f"[MOEX] Unexpected error for stock {ticker}: {e}", exc_info=True)
            return None
    
    @classmethod
    def get_instrument_info(cls, ticker: str, sec_type: str = 'futures') -> Optional[Dict[str, Any]]:
        """
        Универсальный метод для получения параметров инструмента.
        
        Args:
            ticker: Тикер инструмента
            sec_type: Тип инструмента ('futures' или 'stock')
            
        Returns:
            Словарь с параметрами инструмента или None при ошибке
        """
        if sec_type == 'futures':
            return cls.get_futures_info(ticker)
        elif sec_type == 'stock':
            return cls.get_stock_info(ticker)
        else:
            logger.error(f"[MOEX] Unknown security type: {sec_type}")
            return None
    
    @classmethod
    def clear_cache(cls, ticker: Optional[str] = None, sec_type: Optional[str] = None):
        """
        Очищает кэш.
        
        Args:
            ticker: Тикер для очистки (если None - очищает весь кэш)
            sec_type: Тип инструмента ('futures' или 'stock')
        """
        with cls._cache_lock:
            if ticker is None:
                cls._cache.clear()
                logger.info("[MOEX] Cache cleared completely")
            else:
                cache_key = f"{sec_type}:{ticker.upper()}" if sec_type else None
                if cache_key and cache_key in cls._cache:
                    del cls._cache[cache_key]
                    logger.info(f"[MOEX] Cache cleared for {cache_key}")
                else:
                    # Очищаем все ключи по тикеру если sec_type не указан
                    to_remove = [k for k in cls._cache if f":{ticker.upper()}" in k]
                    for k in to_remove:
                        del cls._cache[k]
                    if to_remove:
                        logger.info(f"[MOEX] Cache cleared for {ticker}: {to_remove}")
