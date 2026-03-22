"""
Шаблон стратегии для Trading Strategy Manager.
Каждая стратегия ОБЯЗАНА содержать эти функции:
  get_info(), get_params(), on_start(), on_stop(), on_tick()
"""

def get_info() -> dict:
    """Метаданные — отображаются в UI."""
    return {
        "name": "Example Strategy",
        "version": "1.0",
        "author": "Alexey",
        "description": "Шаблон. Замени логику в on_tick().",
        "tickers": ["SBER", "GAZP"],
    }


def get_params() -> dict:
    """
    Описание параметров для UI-редактора.
    Поддерживаемые типы: int, float, bool, str, choice
    """
    return {
        "ticker": {
            "type": "ticker",
            "default": "SBER",
            "label": "Тикер",
            "description": "Инструмент для торговли",
        },
        "lot_size": {
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 1000,
            "label": "Размер лота",
            "description": "Количество лотов на вход",
        },
        "stop_loss_pct": {
            "type": "float",
            "default": 1.5,
            "min": 0.1,
            "max": 10.0,
            "label": "Стоп-лосс (%)",
            "description": "Процент от цены входа",
        },
        "use_market_order": {
            "type": "bool",
            "default": True,
            "label": "Рыночный ордер",
            "description": "True = рыночный, False = лимитный",
        },
        "order_type": {
            "type": "choice",
            "default": "market",
            "options": ["market", "limit", "stop"],
            "label": "Тип ордера",
        },
    }


def on_start(params: dict, connector) -> None:
    """Вызывается при запуске стратегии."""
    print(f"[{get_info()['name']}] Запуск. Параметры: {params}")


def on_stop(params: dict, connector) -> None:
    """Вызывается при остановке стратегии."""
    print(f"[{get_info()['name']}] Остановка.")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    """
    Вызывается при каждом тике данных.
    tick_data = {"ticker": "SBER", "price": 280.5, "volume": 1000}
    connector  = объект FinamConnector (появится в Этапе 4)
    """
    signal = _calculate_signal(tick_data, params)

    if signal == "BUY":
        connector.place_order(
            ticker=params["ticker"],
            side="buy",
            quantity=params["lot_size"],
            order_type=params["order_type"],
        )
    elif signal == "SELL":
        connector.place_order(
            ticker=params["ticker"],
            side="sell",
            quantity=params["lot_size"],
            order_type=params["order_type"],
        )


def _calculate_signal(tick_data: dict, params: dict) -> str | None:
    """Сюда пишешь свою торговую логику. Верни 'BUY', 'SELL' или None."""
    return None
