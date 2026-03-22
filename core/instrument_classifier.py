"""
Классификатор инструментов для расчёта комиссий.

Определяет тип инструмента по тикеру и борде с использованием трёх уровней приоритета:
1. Ручной маппинг из конфига (высший приоритет)
2. Правила по префиксу тикера
3. Борд как подсказка (запасной вариант)
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class InstrumentClassifier:
    """
    Классификатор инструментов для определения типа и формулы расчёта комиссии.
    
    Типы инструментов делятся на две группы:
    
    Группа фьючерсов (брокер в рублях абс.):
    - currency_futures — валютные (Si, CR, USDRUB)
    - equity_futures — фондовые (SBER, GAZP в секции фьючерсов)
    - index_futures — индексные (RI, MX, MM)
    - commodity_futures — товарные (BR, NG, GD, GLD, SIL)
    
    Группа акций/облигаций (брокер в процентах):
    - stock — акции
    - bond — облигации
    - etf — фонды
    """
    
    FUTURES_TYPES = {
        "currency_futures",
        "equity_futures",
        "index_futures",
        "commodity_futures"
    }
    
    STOCK_TYPES = {
        "stock",
        "bond",
        "etf"
    }
    
    ALL_TYPES = FUTURES_TYPES | STOCK_TYPES
    
    def __init__(self, config_path: str = "data/commission_config.json"):
        """
        Инициализация классификатора.
        
        Args:
            config_path: Путь к файлу конфигурации комиссий
        """
        self.config_path = Path(config_path)
        self.manual_mapping: dict[str, str] = {}
        self.prefix_rules: dict[str, str] = {}
        self._load_config()
    
    def _load_config(self):
        """Загружает правила классификации из конфига."""
        try:
            if not self.config_path.exists():
                logger.warning(f"[InstrumentClassifier] Конфиг не найден: {self.config_path}")
                return
            
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            
            self.manual_mapping = config.get("manual_mapping", {})
            self.prefix_rules = config.get("prefix_rules", {})
            
            logger.debug(f"[InstrumentClassifier] Загружено правил: "
                        f"manual={len(self.manual_mapping)}, prefix={len(self.prefix_rules)}")
        
        except Exception as e:
            logger.error(f"[InstrumentClassifier] Ошибка загрузки конфига: {e}")
    
    def classify(self, ticker: str, board: str = "TQBR") -> str:
        """
        Определяет тип инструмента по тикеру и борде.
        
        Приоритет:
        1. Ручной маппинг (manual_mapping)
        2. Правила по префиксу (prefix_rules)
        3. Борд как подсказка (FUT -> equity_futures, TQBR -> stock)
        
        Args:
            ticker: Тикер инструмента
            board: Код борды (TQBR, SPBFUT и т.д.)
        
        Returns:
            Тип инструмента (один из ALL_TYPES)
        """
        if not ticker:
            return "stock"  # Дефолт
        
        ticker_upper = ticker.upper()
        board_upper = board.upper()
        
        # Уровень 1: Ручной маппинг (высший приоритет)
        # НО: если борд явно фьючерсный (FUT) — игнорируем маппинг на stock/bond/etf
        if ticker_upper in self.manual_mapping:
            result = self.manual_mapping[ticker_upper]
            # Если борд фьючерсный и результат — тип акции/облигации, проваливаемся дальше
            if "FUT" in board_upper and result in self.STOCK_TYPES:
                pass  # игнорируем manual_mapping, используем prefix_rules
            else:
                logger.debug(f"[InstrumentClassifier] {ticker} -> {result} (manual)")
                return result
        
        # Уровень 2: Правила по префиксу
        for prefix, instrument_type in self.prefix_rules.items():
            if ticker_upper.startswith(prefix.upper()):
                logger.debug(f"[InstrumentClassifier] {ticker} -> {instrument_type} (prefix: {prefix})")
                return instrument_type
        
        # Уровень 3: Борд как подсказка
        if "FUT" in board_upper:
            logger.debug(f"[InstrumentClassifier] {ticker} -> equity_futures (board: {board})")
            return "equity_futures"
        elif board_upper in ("TQBR", "TQTF", "TQOB"):
            # TQBR - акции, TQTF - ETF, TQOB - облигации
            if board_upper == "TQTF":
                return "etf"
            elif board_upper == "TQOB":
                return "bond"
            else:
                return "stock"
        
        # Дефолт - акция
        logger.debug(f"[InstrumentClassifier] {ticker} -> stock (default)")
        return "stock"
    
    def is_futures(self, ticker: str, board: str = "TQBR") -> bool:
        """
        Быстрая проверка - является ли инструмент фьючерсом.
        
        Args:
            ticker: Тикер инструмента
            board: Код борды
        
        Returns:
            True если инструмент - фьючерс, False иначе
        """
        instrument_type = self.classify(ticker, board)
        return instrument_type in self.FUTURES_TYPES
    
    def get_group(self, ticker: str, board: str = "TQBR") -> str:
        """
        Возвращает группу инструмента (futures или stock).
        
        Args:
            ticker: Тикер инструмента
            board: Код борды
        
        Returns:
            "futures" или "stock"
        """
        return "futures" if self.is_futures(ticker, board) else "stock"
    
    def add_manual_mapping(self, ticker: str, instrument_type: str):
        """
        Добавляет ручной маппинг тикера.
        
        Args:
            ticker: Тикер инструмента
            instrument_type: Тип инструмента
        """
        if instrument_type not in self.ALL_TYPES:
            raise ValueError(f"Неизвестный тип инструмента: {instrument_type}")
        
        self.manual_mapping[ticker.upper()] = instrument_type
        logger.info(f"[InstrumentClassifier] Добавлен маппинг: {ticker} -> {instrument_type}")
    
    def add_prefix_rule(self, prefix: str, instrument_type: str):
        """
        Добавляет правило по префиксу.
        
        Args:
            prefix: Префикс тикера
            instrument_type: Тип инструмента
        """
        if instrument_type not in self.ALL_TYPES:
            raise ValueError(f"Неизвестный тип инструмента: {instrument_type}")
        
        self.prefix_rules[prefix.upper()] = instrument_type
        logger.info(f"[InstrumentClassifier] Добавлено правило: {prefix}* -> {instrument_type}")
    
    def remove_manual_mapping(self, ticker: str):
        """Удаляет ручной маппинг тикера."""
        ticker_upper = ticker.upper()
        if ticker_upper in self.manual_mapping:
            del self.manual_mapping[ticker_upper]
            logger.info(f"[InstrumentClassifier] Удалён маппинг: {ticker}")
    
    def remove_prefix_rule(self, prefix: str):
        """Удаляет правило по префиксу."""
        prefix_upper = prefix.upper()
        if prefix_upper in self.prefix_rules:
            del self.prefix_rules[prefix_upper]
            logger.info(f"[InstrumentClassifier] Удалено правило: {prefix}")
    
    def save_config(self):
        """Сохраняет текущие правила в конфиг."""
        try:
            # Читаем существующий конфиг
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            
            # Обновляем правила
            config["manual_mapping"] = self.manual_mapping
            config["prefix_rules"] = self.prefix_rules
            
            # Сохраняем
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            logger.info(f"[InstrumentClassifier] Конфиг сохранён: {self.config_path}")
        
        except Exception as e:
            logger.error(f"[InstrumentClassifier] Ошибка сохранения конфига: {e}")


# Глобальный экземпляр классификатора
instrument_classifier = InstrumentClassifier()
