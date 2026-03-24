# Trading Strategy Manager

Desktop-приложение PyQt6 для управления торговыми стратегиями на Московской бирже.
Два брокера: **Финам** (TransAQ/txmlconnector64.dll через ctypes) и **QUIK** (QuikPy/Lua).

## Правила разработки

@.claude/rules.md

## Документация (читать по ситуации)

- **Перед написанием нового кода** → `docs/patterns.md` (14 готовых паттернов)
- **При отладке / странном поведении** → `docs/known_issues.md` (14 известных проблем с решениями)
- **Новая стратегия** → `/new-strategy`, шаблон: `strategies/_template.py`
- **Новый коннектор** → `/new-connector`
- **Проверка синтаксиса** → `/check`

---

## Стек

PyQt6 · pyqtgraph · loguru · APScheduler · python-telegram-bot · PyInstaller
Данные: JSON в `data/` (без БД) · MOEX ISS REST API · Catppuccin Mocha (тема)

## Карта файлов

```
core/
  base_connector.py      # ABC — наследовать для новых брокеров
  finam_connector.py     # TransAQ DLL, ctypes, callback-архитектура
  quik_connector.py      # QUIK через QuikPy, polling
  connector_manager.py   # Реестр коннекторов (синглтон)
  live_engine.py         # Реальная торговля: polling → on_bar → execute
  backtest_engine.py     # Бэктест: TXT → Bar → on_bar (close[i]→open[i+1])
  commission_manager.py  # Расчёт комиссий (синглтон)
  instrument_classifier.py  # Тип инструмента: manual_mapping→prefix→board
  moex_api.py            # MOEX ISS API, кэш 4ч(фьючерсы)/24ч(акции)
  order_history.py       # История ордеров, FIFO matching, PnL
  equity_tracker.py      # Просадка агента, flush каждые 30с
  storage.py             # JSON I/O: атомарная запись, кэш TTL=2с, .bak
  strategy_loader.py     # Динамическая загрузка .py, circuit breaker
  position_manager.py    # Менеджер позиций (синглтон)
  scheduler.py           # APScheduler расписание коннекторов
  autostart.py           # Автозапуск при старте приложения
  chase_order.py         # Лимитка по стакану с автоперестановкой
  chart_cache.py         # Pickle-кэш свечей на диске
  telegram_bot.py        # Уведомления (синглтон notifier)
  txt_loader.py          # Парсер FINAM TXT: OHLCV → Bar

ui/
  main_window.py         # Главное окно, таблица агентов, ui_signals
  strategy_window.py     # Окно агента: обзор / параметры / лотность / позиции
  param_widgets.py       # ParamWidgetFactory + BaseParamWidget наследники
  chart_window.py        # pyqtgraph: свечи, индикаторы, crosshair, DataLoader
  backtest_window.py     # Диалог запуска бэктеста
  backtest_report.py     # Отчёт: equity curve, таблица сделок, CSV
  settings_window.py     # _SettingsMixin → SettingsWindow + SettingsWidget
  positions_panel.py     # Панель позиций с кнопками закрытия
  ticker_selector.py     # Виджет борд + тикер с автозагрузкой
  instruments_editor.py  # Редактор корзины инструментов (Achilles)
  tray.py                # Системный трей

strategies/
  _template.py           # Шаблон — копировать для новой стратегии
  achilles.py            # Mean Reversion, корзина, execute_signal
  bochka_cny.py          # Пробой Highest/Lowest, фьючерс CNY
  daytrend.py            # Пробой дневного диапазона
  tracker.py             # Канал SMA±ATR, лимитки
  valera_trend.py        # Тренд по SMA

data/                    # НЕ редактировать вручную
  commission_config.json # Ставки + manual_mapping + prefix_rules
  strategies.json        # Конфиги агентов
  order_history.json     # Критично для PnL — не трогать
```

## Интерфейс стратегии

```python
# Обязательные (проверяет strategy_loader):
def get_info() -> dict
def get_params() -> dict        # схема параметров → автогенерация UI
def on_start(params, connector)
def on_stop(params, connector)
def on_tick(tick_data, params, connector)

# Опциональные (баровые стратегии):
def on_precalc(df, params) -> df          # pandas only, O(n), без Python-циклов!
def on_bar(bars, position, params) -> dict # {"action":"buy"|"sell"|"close"|None, "qty":int, "comment":str}
def get_lookback(params) -> int
def execute_signal(signal, connector, params, account_id)  # мультиинструментальные
def get_indicators() -> list[dict]        # {"col":"_sma","type":"line","color":"#89b4fa",...}
```

## Синглтоны

```python
from core.connector_manager    import connector_manager    # .get("finam"), .get("quik")
from core.commission_manager   import commission_manager   # .calculate(...)
from core.instrument_classifier import instrument_classifier
from core.position_manager     import position_manager
from core.strategy_loader      import strategy_loader
from core.scheduler            import strategy_scheduler
from core.telegram_bot         import notifier, EventCode
from core.finam_connector      import finam_connector
from core.quik_connector       import quik_connector
```

> Коннекторы регистрируются через `register_connectors()` **после** инициализации UI, не при импорте.

## Формулы

```
PnL        = (price - entry_price) × qty × point_cost
Комиссия фьючерс = price × point_cost × qty × moex_pct%  +  broker_rub × qty
Комиссия акция   = price × qty × (moex_pct + broker_pct)%

point_cost:  MOEX API → DLL (приоритет! DLL часто возвращает неверные данные)
commission в order_history:  руб/лот, ОДНА сторона (не round-trip)
```

## Соглашения

| Что | Значение |
|-----|----------|
| Таймфреймы | строки `"1"` `"5"` `"15"` `"30"` `"60"` (минуты) |
| Время в параметрах | минуты от полуночи: `600`=10:00, `1425`=23:45 |
| Борд фьючерсов | `"FUT"` (Финам) / `"SPBFUT"` (QUIK) |
| ID коннекторов | `"finam"`, `"quik"` — строго lowercase |
| Межпоточные обновления UI | только через `ui_signals.*`, никогда напрямую |
| FIFO matching | `get_order_pairs()` в `order_history.py` |

## Типы параметров (ParamWidgetFactory)

`str` QLineEdit · `int` QSpinBox · `float` QDoubleSpinBox · `bool` QCheckBox · `time` QTimeEdit
`select`/`choice` QComboBox · `timeframe` QComboBox-пресеты · `ticker` TickerSelector
`instruments` _InstrumentsWidget · `commission` CommissionParamWidget (авто / % / ₽)

## Команды

```bash
python main.py                                                   # запуск
python -m py_compile <file>.py                                   # проверка синтаксиса
.venv/Scripts/python.exe -m PyInstaller -y trading_manager.spec  # сборка exe
```
