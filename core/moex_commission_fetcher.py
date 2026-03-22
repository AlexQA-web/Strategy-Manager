# core/moex_commission_fetcher.py

"""
Модуль для автоматического получения актуальных ставок комиссий MOEX.
Использует публичное API MOEX для загрузки тарифов биржи.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from loguru import logger


class MOEXCommissionFetcher:
    """
    Класс для получения актуальных ставок комиссий MOEX через API.
    
    Кэширует результаты на 24 часа для минимизации запросов к API.
    """
    
    # URL для получения информации о тарифах MOEX
    # Примечание: это упрощённая реализация, т.к. MOEX не предоставляет прямого API для тарифов
    # В реальности нужно парсить страницы или использовать другие источники
    MOEX_TARIFFS_URL = "https://www.moex.com/ru/tariffs/"
    
    def __init__(self, cache_file: str = "data/moex_commission_cache.json"):
        self.cache_file = Path(cache_file)
        self.cache_ttl = timedelta(hours=24)
    
    def fetch_rates(self) -> Optional[dict]:
        """
        Получает актуальные ставки комиссий MOEX.
        
        Returns:
            Словарь с ставками или None при ошибке
        """
        # Проверяем кэш
        cached = self._load_cache()
        if cached and self._is_cache_valid(cached):
            logger.debug("Используются закэшированные ставки MOEX")
            return cached["rates"]
        
        # Пытаемся загрузить новые ставки
        try:
            rates = self._fetch_from_moex()
            if rates:
                self._save_cache(rates)
                return rates
        except Exception as e:
            logger.warning(f"Ошибка при загрузке ставок MOEX: {e}")
        
        # Если не удалось загрузить, возвращаем устаревший кэш (если есть)
        if cached:
            logger.warning("Используются устаревшие ставки MOEX из кэша")
            return cached["rates"]
        
        return None
    
    def _fetch_from_moex(self) -> Optional[dict]:
        """
        Загружает ставки с сайта MOEX.
        
        ВАЖНО: Это заглушка! В реальной реализации нужно:
        1. Парсить HTML страницу тарифов MOEX
        2. Или использовать альтернативный источник данных
        3. Или получать данные от брокера через API
        
        Returns:
            Словарь с актуальными ставками
        """
        logger.info("Попытка загрузки актуальных ставок MOEX...")
        
        # ЗАГЛУШКА: В реальности здесь должен быть парсинг или API запрос
        # Для демонстрации возвращаем None, что означает "не удалось загрузить"
        # 
        # Пример реальной реализации:
        # response = requests.get(self.MOEX_TARIFFS_URL, timeout=10)
        # if response.status_code == 200:
        #     # Парсинг HTML или JSON
        #     rates = self._parse_tariffs(response.text)
        #     return rates
        
        logger.warning(
            "Автоматическая загрузка ставок MOEX не реализована. "
            "Используются ставки из конфигурации."
        )
        return None
    
    def _load_cache(self) -> Optional[dict]:
        """Загружает кэш из файла."""
        if not self.cache_file.exists():
            return None
        
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка чтения кэша ставок MOEX: {e}")
            return None
    
    def _save_cache(self, rates: dict) -> None:
        """Сохраняет ставки в кэш."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "rates": rates
            }
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
            logger.debug(f"Ставки MOEX сохранены в кэш: {self.cache_file}")
        except Exception as e:
            logger.warning(f"Ошибка сохранения кэша ставок MOEX: {e}")
    
    def _is_cache_valid(self, cached: dict) -> bool:
        """Проверяет, актуален ли кэш."""
        try:
            timestamp = datetime.fromisoformat(cached["timestamp"])
            age = datetime.now() - timestamp
            return age < self.cache_ttl
        except Exception:
            return False
    
    def get_cache_age(self) -> Optional[timedelta]:
        """
        Возвращает возраст кэша.
        
        Returns:
            Возраст кэша или None, если кэш отсутствует
        """
        cached = self._load_cache()
        if not cached:
            return None
        
        try:
            timestamp = datetime.fromisoformat(cached["timestamp"])
            return datetime.now() - timestamp
        except Exception:
            return None
    
    def is_cache_outdated(self) -> bool:
        """
        Проверяет, устарел ли кэш.
        
        Returns:
            True, если кэш устарел или отсутствует
        """
        age = self.get_cache_age()
        if age is None:
            return True
        return age >= self.cache_ttl


# Глобальный экземпляр
moex_commission_fetcher = MOEXCommissionFetcher()
