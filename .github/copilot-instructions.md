# Project Guidelines

## Overview

Trading Strategy Manager — десктопное PyQt6-приложение для автоматизированной торговли на Московской бирже. Подключается к терминалам QUIK и Transaq, исполняет пользовательские стратегии в реальном времени и в бэктесте.

## Architecture

- **Layered**: `core/` (бизнес-логика) → `ui/` (PyQt6 GUI) → `strategies/` (модули стратегий)
- **Правило импортов**: `core/` **никогда** не импортирует `ui/`. Обратная связь — только через Qt-сигналы (`ui_signals`)
- **DI**: `core.di_container.container` — глобальный контейнер, `register()` / `resolve()` с поддержкой синглтонов и фабрик
- **Коннекторы**: `BaseConnector` (абстрактный) → `FinamConnector` (Transaq XML/ctypes), `QuikConnector`. Регистрация отложена, чтобы избежать побочных эффектов при импорте
- **Storage**: JSON-файлы с RWLock, 2s TTL-кэш, атомарная запись (`.tmp` → rename), 3-уровневый бэкап (`.bak`, `.bak2`, `.bak3`)

### Потоки данных (Live)

```
LiveEngine → connector.get_history()
           → strategy.on_precalc(df)
           → strategy.on_bar(bars, position) → signal
           → OrderExecutor.execute_signal()
           → TradeRecorder → order_history.json
```

Подробнее: [docs/decisions.md](../docs/decisions.md) (ADR), [core/REFERENCE.md](../core/REFERENCE.md) (API синглтонов)

## Build and Test

```bash
# Установка
pip install -r requirements.txt

# Запуск
python main.py

# Тесты
pytest tests/ -v

# Сборка (Windows, PyInstaller)
build.bat
```

## Code Style

- Python 3.10+, type hints рекомендуются
- Логирование: `loguru.logger` (не `logging`)
- Потокобезопасность: `RWLock` из `core/rwlock.py` для shared-данных, `threading.Lock` для простых случаев
- Конфигурация: `config/settings.py` — константы, `data/settings.json` — runtime, `app_profile/secrets.local.json` — секреты (DPAPI)

## Conventions

### Стратегии

Обязательный интерфейс модуля:
```python
def get_info() -> dict        # {"name": ..., "description": ...}
def get_params() -> dict      # параметры с типами (str, int, float, bool, time, select, ticker, ...)
def on_start(params, connector) -> None
def on_stop(params, connector) -> None
def on_tick(tick_data, params, connector) -> None
```

Bar-based стратегии дополнительно: `on_precalc(df, params)`, `on_bar(bars, position, params)`, `get_lookback(params)`, `get_indicators()`.

Шаблон: [strategies/_template.py](../strategies/_template.py). Подробности: [strategies/API_REFERENCE.md](../strategies/API_REFERENCE.md), [strategies/REFERENCE.md](../strategies/REFERENCE.md)

### UI / Threading

- **Запрещено** менять виджеты из фоновых потоков напрямую — используй `QTimer.singleShot(0, lambda: ...)` или `ui_signals`
- Тема: Catppuccin Mocha. Цвета определены в `ui/` модулях
- Справка: [ui/REFERENCE.md](../ui/REFERENCE.md)

### Тесты

- pytest + `unittest.mock` (MagicMock, patch)
- Паттерн: mock-зависимости → создать объект → вызов → assert
- Один класс `Test<Component>` на компонент, методы `test_<behavior>`

### Комиссии

- Futures: `trade_value × moex_taker_pct + broker_futures_rub × qty`
- Stocks: `trade_value × (moex_taker_pct + broker_stock_pct)`
- Конфиг: [data/commission_config.json](../data/commission_config.json)

## Key Directories

| Директория | Содержимое |
|---|---|
| `core/` | Бизнес-логика, движки, коннекторы, DI |
| `strategies/` | Торговые стратегии (модули Python) |
| `ui/` | PyQt6 GUI |
| `data/` | JSON runtime-файлы (settings, strategies, orders) |
| `app_profile/` | Секреты, лицензии |
| `conn/` | Нативные библиотеки коннекторов (DLL, Lua) |
| `docs/` | ADR, гайды |
| `tests/` | pytest-тесты |

## Existing Documentation

- [docs/decisions.md](../docs/decisions.md) — архитектурные решения (ctypes vs COM, JSON vs SQLite, polling vs push, и др.)
- [docs/strategy_params_guide.md](../docs/strategy_params_guide.md) — типы параметров и виджеты
- [core/REFERENCE.md](../core/REFERENCE.md) — API модулей core, правила импорта, синглтоны
- [conn/README_Connectors.md](../conn/README_Connectors.md) — архитектура Transaq-коннектора, XML-команды
- [strategies/REFERENCE.md](../strategies/REFERENCE.md) — контракт стратегий, on_bar signal формат
- [strategies/API_REFERENCE.md](../strategies/API_REFERENCE.md) — сигнатуры функций стратегий
- [ui/REFERENCE.md](../ui/REFERENCE.md) — правила потоков UI, палитра, сигналы
