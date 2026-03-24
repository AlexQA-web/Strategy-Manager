# /new-strategy

Создай новую торговую стратегию для Trading Strategy Manager.

## Входные данные от пользователя

Спроси (если не указано в запросе):
1. Название стратегии (рус., для UI)
2. Идентификатор файла (eng., snake_case, без .py)
3. Торговый инструмент: фьючерс или акция?
4. Логика: на барах (`on_bar`) или тиковая (`on_tick`)?

## Что создать

### 1. Файл стратегии `strategies/<id>.py`

Скопируй структуру из `strategies/_template.py` и наполни логикой.

Обязательные функции:
- `get_info()` → dict с name, version, author, description
- `get_params()` → dict со схемой параметров
- `on_start(params, connector)`
- `on_stop(params, connector)`
- `on_tick(tick_data, params, connector)`

Для баровой стратегии добавить:
- `on_precalc(df, params) -> df` — только pandas, без Python-циклов
- `on_bar(bars, position, params) -> dict` — возвращает `{"action": ..., "qty": ..., "comment": ...}`
- `get_lookback(params) -> int`
- `get_indicators() -> list` — для отображения на графике

### 2. Запись в `data/strategies.json`

Добавить запись вида:
```json
"<id>": {
  "name": "<Название>",
  "file_path": "strategies/<id>.py",
  "description": "...",
  "status": "stopped",
  "finam_account": "",
  "is_enabled": true,
  "params": { /* дефолты из get_params() */ },
  "connector": "finam",
  "ticker": "",
  "board": "FUT",
  "timeframe": "5",
  "order_mode": "market",
  "lot_sizing": {"dynamic": false, "lot": 1, "instances": 1, "drawdown": 0.0}
}
```

## Правила

- `on_precalc`: только pandas vectorized (groupby, rolling, shift, merge) — никаких `for bar in bars`
- `on_bar`: только логика сигнала, никаких вызовов коннектора
- Глобальное состояние (если нужно) — сбрасывать в `on_start()` через `reset_state()`
- Комиссию объявлять: `"commission": {"type": "commission", "default": "auto", ...}`
- Тикер объявлять: `"ticker": {"type": "ticker", "default": "...", ...}`
- Время (time_open, time_close) — тип `"time"`, значение в минутах от полуночи
- После создания — `python -m py_compile strategies/<id>.py`

## Проверка синтаксиса

```bash
python -m py_compile strategies/<id>.py
```
