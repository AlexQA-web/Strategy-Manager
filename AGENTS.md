# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Команды (фактически используемые в проекте)
- Запуск GUI: `python main.py`
- Проверка изменённого файла: `python -m py_compile <path/to/file.py>`
- Сборка exe: `build.bat` (скрипт сам активирует `.venv` и вызывает `pyinstaller -y trading_manager.spec`)
- Тестов (pytest/unittest) в репозитории нет; «single test» не поддерживается. Минимальный аналог проверки изменения — `py_compile` на конкретном файле.

## Нестандартные соглашения кода (из .claude/rules.md + кода)
- Комментарии/docstring/логи — на русском; идентификаторы и имена файлов — на английском.
- Для публичных функций/методов обязательны type hints; стиль строк — одинарные кавычки; лимит строки 120.
- Логирование: `loguru` по умолчанию; `logging` допустим только в модулях без зависимости от loguru.
- `print()` в продакшн-коде запрещён.

## Критичные project-specific паттерны
- JSON в `data/` не читать/писать напрямую: использовать `core/storage.py` API (`read_json/write_json/save_setting`).
- Multi-key read-modify-write только под `_write_lock` + `_read(..., use_cache=False)` + `_write_unsafe(...)`.
- UI из фоновых потоков обновлять только через `ui_signals.*` или `QTimer.singleShot(0, ...)`.
- Вход в позицию: проверки `_position` и `_order_in_flight` + установка флага делаются в одном `self._position_lock`.
- Запрещено вкладывать `_chase_lock` и `_position_lock` друг в друга.
- Отмена chase-ордера: сначала дождаться финального статуса, потом `unwatch_order()`.
- `point_cost` для фьючерсов: приоритет MOEX API, затем fallback к данным DLL.
- `FinamConnector.Initialize()` вызывается только один раз за lifecycle (`self._initialized`).
- Коннекторы регистрируются после инициализации UI (`MainWindow()`), не при импорте.
