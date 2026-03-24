# /check

Проверь синтаксис всех Python-файлов проекта, которые были изменены.

## Порядок действий

1. Определи изменённые .py файлы (из контекста разговора или спроси у пользователя)
2. Для каждого файла выполни:

```bash
python -m py_compile <filepath>
```

3. Если есть ошибки — покажи их и исправь
4. Повтори проверку после исправления
5. Подтверди: "✅ Все файлы прошли проверку синтаксиса"

## Типичные файлы для проверки

```bash
python -m py_compile core/live_engine.py
python -m py_compile core/backtest_engine.py
python -m py_compile core/storage.py
python -m py_compile core/order_history.py
python -m py_compile strategies/<changed>.py
python -m py_compile ui/strategy_window.py
python -m py_compile ui/param_widgets.py
python -m py_compile ui/main_window.py
```

## Если проект собирается через venv

```bash
.venv/Scripts/python.exe -m py_compile <filepath>
```

## После проверки

Если всё ОК — предложи следующий шаг (запуск, тест, коммит).
