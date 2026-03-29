"""
Менеджер расчёта комиссий.

Центральный синглтон для расчёта комиссий по всем типам инструментов.
Использует классификатор инструментов и конфигурацию ставок.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from core.instrument_classifier import instrument_classifier
from core.moex_api import MOEXClient
from core.storage import read_json, write_json


class CommissionManager:
    """
    Менеджер расчёта комиссий для всех типов инструментов.
    
    Формулы расчёта:
    
    Для фьючерсов:
        trade_value = price × point_cost × quantity
        moex_part = trade_value × moex_taker_pct / 100
        broker_part = broker_futures_rub × quantity
        итог = moex_part + broker_part
    
    Для акций / облигаций / ETF:
        trade_value = price × lot_size × quantity
        moex_part = trade_value × moex_taker_pct / 100
        broker_part = trade_value × broker_stock_pct / 100
        итог = moex_part + broker_part
    
    При order_role == "maker": moex_part = 0, итог = только broker_part
    """
    
    def __init__(self, config_path: str = "data/commission_config.json"):
        """
        Инициализация менеджера комиссий.
        
        Args:
            config_path: Путь к файлу конфигурации
        """
        self.config_path = Path(config_path)
        self.config: dict = {}
        self._load_config()
    
    def _load_config(self):
        """Загружает конфигурацию ставок."""
        try:
            if not self.config_path.exists():
                logger.warning(f"[CommissionManager] Конфиг не найден: {self.config_path}")
                self._create_default_config()
                self.save_config()
                return

            loaded = read_json(self.config_path)
            self.config = loaded if isinstance(loaded, dict) else {}
            if not self.config:
                logger.warning(f"[CommissionManager] Пустой/битый конфиг: {self.config_path}")
                self._create_default_config()
                self.save_config()
                return
            
            logger.debug(f"[CommissionManager] Конфиг загружен: {self.config_path}")
        
        except Exception as e:
            logger.error(f"[CommissionManager] Ошибка загрузки конфига: {e}")
            self._create_default_config()
    
    def _create_default_config(self):
        """Создаёт конфиг по умолчанию."""
        self.config = {
            "moex": {
                "taker_pct": {
                    "currency_futures": 0.001,
                    "equity_futures": 0.003,
                    "index_futures": 0.001,
                    "commodity_futures": 0.005,
                    "stock": 0.003,
                    "bond": 0.003,
                    "etf": 0.003
                },
                "maker_pct": {
                    "currency_futures": 0.0,
                    "equity_futures": 0.0,
                    "index_futures": 0.0,
                    "commodity_futures": 0.0,
                    "stock": 0.0,
                    "bond": 0.0,
                    "etf": 0.0
                }
            },
            "broker_transaq": {
                "futures_rub": {
                    "currency_futures": 1.00,
                    "equity_futures": 0.45,
                    "index_futures": 0.87,
                    "commodity_futures": 2.10
                },
                "stock_pct": 0.04,
                "bond_pct": 0.015,
                "etf_pct": 0.04
            },
            "broker_quik": {
                "futures_rub": {
                    "currency_futures": 1.00,
                    "equity_futures": 0.45,
                    "index_futures": 0.87,
                    "commodity_futures": 2.10
                },
                "stock_pct": 0.04,
                "bond_pct": 0.015,
                "etf_pct": 0.04
            },
            "last_moex_update": datetime.now().strftime("%Y-%m-%d")
        }
    
    def calculate(
        self,
        ticker: str,
        board: str,
        quantity: float,
        price: float,
        order_role: str = 'taker',
        point_cost: Optional[float] = None,
        connector_id: str = 'transaq',
        lot_size: Optional[int] = None,
    ) -> float:
        """
        Рассчитывает комиссию за одну сторону сделки.
        
        Args:
            ticker: Тикер инструмента
            board: Код борды
            quantity: Количество контрактов/лотов
            price: Цена
            order_role: Роль ордера ("taker" или "maker")
            point_cost: Стоимость пункта (для фьючерсов)
            connector_id: ID коннектора ("transaq" или "quik")
            lot_size: Размер лота для акций/облигаций/ETF; если None, пытаемся
                      определить автоматически
        
        Returns:
            Комиссия в рублях за одну сторону
        """
        # Определяем тип инструмента и группу за один вызов
        instrument_type = instrument_classifier.classify(ticker, board)
        is_futures = instrument_type in instrument_classifier.FUTURES_TYPES

        # Получаем ставки
        moex_pct = self._get_moex_rate(instrument_type, order_role)

        # Определяем конфигурацию брокера в зависимости от коннектора
        broker_config = self._get_broker_config(connector_id)
        
        if is_futures:
            # Для фьючерсов
            if point_cost is None or point_cost == 0:
                logger.warning(f"[CommissionManager] point_cost не указан для {ticker}, используем 1.0")
                point_cost = 1.0
            
            trade_value = price * point_cost * quantity
            moex_part = trade_value * moex_pct / 100
            
            broker_rub = broker_config.get("futures_rub", {}).get(instrument_type, 1.0)
            broker_part = broker_rub * quantity
            
            total = moex_part + broker_part
            
            logger.debug(f"[CommissionManager] {ticker}: futures, connector={connector_id}, "
                        f"trade_value={trade_value:.2f}, moex={moex_part:.2f}, "
                        f"broker={broker_part:.2f}, total={total:.2f}")
        else:
            # Для акций/облигаций/ETF quantity приходит в лотах,
            # поэтому комиссия должна считаться от полной денежной суммы сделки.
            resolved_lot_size = self._resolve_lot_size(ticker, board, lot_size)
            trade_value = price * quantity * resolved_lot_size
            moex_part = trade_value * moex_pct / 100
            
            # Получаем процентную ставку брокера в зависимости от коннектора
            broker_pct = broker_config.get(f'{instrument_type}_pct', 0.04)
            broker_part = trade_value * broker_pct / 100
            
            total = moex_part + broker_part
            
            logger.debug(
                f'[CommissionManager] {ticker}: stock, connector={connector_id}, '
                f'lot_size={resolved_lot_size}, trade_value={trade_value:.2f}, '
                f'moex={moex_part:.2f}, broker={broker_part:.2f}, total={total:.2f}'
            )
        
        return total
    
    def get_breakdown(
        self,
        ticker: str,
        board: str,
        quantity: float,
        price: float,
        order_role: str = 'taker',
        point_cost: Optional[float] = None,
        connector_id: str = 'transaq',
        lot_size: Optional[int] = None,
    ) -> dict:
        """
        Возвращает детализированный расчёт комиссии.
        
        Args:
            ticker: Тикер инструмента
            board: Код борды
            quantity: Количество контрактов/лотов
            price: Цена
            order_role: Роль ордера ("taker" или "maker")
            point_cost: Стоимость пункта (для фьючерсов)
            connector_id: ID коннектора ("transaq" или "quik")
            lot_size: Размер лота для акций/облигаций/ETF
        
        Returns:
            Словарь с детализацией расчёта
        """
        # Определяем тип инструмента и группу за один вызов
        instrument_type = instrument_classifier.classify(ticker, board)
        is_futures = instrument_type in instrument_classifier.FUTURES_TYPES

        # Получаем ставки
        moex_pct = self._get_moex_rate(instrument_type, order_role)

        # Определяем конфигурацию брокера в зависимости от коннектора
        broker_config = self._get_broker_config(connector_id)
        
        if is_futures:
            # Для фьючерсов
            if point_cost is None or point_cost == 0:
                point_cost = 1.0
            
            trade_value = price * point_cost * quantity
            moex_rub = trade_value * moex_pct / 100
            
            broker_rub = broker_config.get("futures_rub", {}).get(instrument_type, 1.0)
            broker_total = broker_rub * quantity
            
            total_one_side = moex_rub + broker_total
            
            return {
                "instrument_type": instrument_type,
                "is_futures": True,
                "trade_value": trade_value,
                "moex_pct": moex_pct,
                "moex_rub": moex_rub,
                "broker_rub": broker_rub,
                "broker_pct": None,
                "total_one_side": total_one_side,
                "total_roundtrip": total_one_side * 2,
                "order_role": order_role,
                "connector_id": connector_id
            }
        else:
            # Для акций/облигаций/ETF quantity приходит в лотах.
            resolved_lot_size = self._resolve_lot_size(ticker, board, lot_size)
            trade_value = price * quantity * resolved_lot_size
            moex_rub = trade_value * moex_pct / 100
            
            # Получаем процентную ставку брокера в зависимости от коннектора
            broker_pct = broker_config.get(f'{instrument_type}_pct', 0.04)
            broker_rub = trade_value * broker_pct / 100
            
            total_one_side = moex_rub + broker_rub
            
            return {
                'instrument_type': instrument_type,
                'is_futures': False,
                'trade_value': trade_value,
                'lot_size': resolved_lot_size,
                'moex_pct': moex_pct,
                'moex_rub': moex_rub,
                'broker_rub': broker_rub,
                'broker_pct': broker_pct,
                'total_one_side': total_one_side,
                'total_roundtrip': total_one_side * 2,
                'order_role': order_role,
                'connector_id': connector_id
            }
    
    def effective_rate_pct(
        self,
        ticker: str,
        board: str,
        order_role: str = "taker",
        connector_id: str = "transaq"
    ) -> float:
        """
        Возвращает эффективную процентную ставку комиссии.
        
        Для фьючерсов возвращает только процентную часть MOEX,
        т.к. брокерская часть в рублях зависит от количества.
        
        Для акций возвращает сумму MOEX + брокер в процентах.
        
        Args:
            ticker: Тикер инструмента
            board: Код борды
            order_role: Роль ордера ("taker" или "maker")
            connector_id: ID коннектора ("transaq" или "quik")
        
        Returns:
            Эффективная ставка в процентах
        """
        instrument_type = instrument_classifier.classify(ticker, board)
        is_futures = instrument_type in instrument_classifier.FUTURES_TYPES

        moex_pct = self._get_moex_rate(instrument_type, order_role)

        if is_futures:
            # Для фьючерсов возвращаем только MOEX%
            return moex_pct
        else:
            # Для акций возвращаем сумму MOEX + брокер
            broker_config = self._get_broker_config(connector_id)
            broker_pct = broker_config.get(f"{instrument_type}_pct", 0.04)
            return moex_pct + broker_pct
    
    def _resolve_lot_size(self, ticker: str, board: str, lot_size: Optional[int]) -> int:
        """Возвращает размер лота для акций/облигаций/ETF."""
        if lot_size is not None:
            try:
                parsed = int(lot_size)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass

        try:
            moex_info = MOEXClient.get_instrument_info(ticker, sec_type='stock')
            if moex_info:
                parsed = int(moex_info.get('lot_size') or 1)
                if parsed > 0:
                    return parsed
        except Exception as e:
            logger.debug(f'[CommissionManager] lot_size lookup error for {ticker}.{board}: {e}')

        logger.debug(f'[CommissionManager] lot_size for {ticker}.{board} not found, используем 1')
        return 1

    def _get_moex_rate(self, instrument_type: str, order_role: str) -> float:
        """Получает ставку MOEX для типа инструмента и роли ордера."""
        role_key = 'maker_pct' if order_role == 'maker' else 'taker_pct'
        return self.config.get('moex', {}).get(role_key, {}).get(instrument_type, 0.0)

    def _get_broker_config(self, connector_id: str) -> dict:
        """Возвращает конфиг брокера по ID коннектора.

        Известные ID: "quik" → broker_quik, всё остальное → broker_transaq.
        При неизвестном ID выводит предупреждение — это помогает обнаружить
        проблему при добавлении нового брокера.
        """
        cid = connector_id.lower()
        if cid == "quik":
            return self.config.get("broker_quik", {})
        if cid in ("transaq", "finam"):
            return self.config.get("broker_transaq", {})
        logger.warning(
            f"[CommissionManager] Неизвестный connector_id='{connector_id}', "
            f"используется конфиг broker_transaq по умолчанию"
        )
        return self.config.get("broker_transaq", {})
    
    def load_config(self):
        """Перезагружает конфигурацию из файла."""
        self._load_config()
    
    def save_config(self):
        """Сохраняет текущую конфигурацию в файл."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.config_path, self.config)
            
            logger.info(f"[CommissionManager] Конфиг сохранён: {self.config_path}")
        
        except Exception as e:
            logger.error(f"[CommissionManager] Ошибка сохранения конфига: {e}")
    
    def update_moex_rates(self, rates: dict):
        """
        Обновляет ставки MOEX.
        
        Args:
            rates: Словарь {instrument_type: taker_pct}
        """
        if "moex" not in self.config:
            self.config["moex"] = {"taker_pct": {}, "maker_pct": {}}
        
        for instrument_type, rate in rates.items():
            self.config["moex"]["taker_pct"][instrument_type] = rate
        
        self.config["last_moex_update"] = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"[CommissionManager] Обновлены ставки MOEX: {rates}")
    
    def get_last_update_date(self) -> Optional[str]:
        """Возвращает дату последнего обновления ставок MOEX."""
        return self.config.get("last_moex_update")
    
    def days_since_update(self) -> Optional[int]:
        """Возвращает количество дней с последнего обновления ставок."""
        last_update = self.get_last_update_date()
        if not last_update:
            return None
        
        try:
            last_date = datetime.strptime(last_update, "%Y-%m-%d")
            delta = datetime.now() - last_date
            return delta.days
        except Exception:
            return None


# Глобальный экземпляр менеджера комиссий
commission_manager = CommissionManager()
