# Отчёт по проверке пунктов со статусом «ПРАВДА»

Проверены только пункты, которые в исходном списке были помечены как «ПРАВДА». Пункты со статусом «ГАЛЛЮЦИНАЦИЯ» сознательно не проверялись.

## Сводка

- Подтверждаю: 33 пункта.
- Частично подтверждаю: 1 пункт.
- Не подтверждаю: 4 пункта.

---

## [`core/storage.py`](core/storage.py)

### 1. TOCTOU в [`_read()`](core/storage.py:19)
**Вердикт:** подтверждаю.

**Основание:** значение mtime читается на [`core/storage.py:28`](core/storage.py:28) до входа в [`_cache_lock`](core/storage.py:13), а затем используется внутри критической секции на [`core/storage.py:31-37`](core/storage.py:31). Между этими действиями состояние файла и кэша может измениться. Формально это действительно окно TOCTOU.

### 2. Проблема производительности в [`append_trade()`](core/storage.py:197)
**Вердикт:** подтверждаю.

**Основание:** внутри [`append_trade()`](core/storage.py:197) под [`_write_lock`](core/storage.py:12) выполняется полный read-modify-write: чтение всего файла на [`core/storage.py:205`](core/storage.py:205), добавление элемента на [`core/storage.py:208`](core/storage.py:208) и полная перезапись JSON на [`core/storage.py:212`](core/storage.py:212). При частых сделках это даёт лишний I/O и удерживает общий lock дольше, чем хотелось бы.

### 3. Слишком короткий [`_CACHE_TTL`](core/storage.py:15)
**Вердикт:** подтверждаю.

**Основание:** TTL равен 2 секундам на [`core/storage.py:15`](core/storage.py:15), а интервал обновления позиций задан как [`POSITIONS_REFRESH_INTERVAL = 5`](config/settings.py:29). При таком соотношении кэш будет истекать раньше следующего типичного опроса, что действительно повышает вероятность лишних чтений с диска.

---

## [`core/order_history.py`](core/order_history.py)

### 4. [`update_order_pnl()`](core/order_history.py:95) — мёртвый код
**Вердикт:** подтверждаю.

**Основание:** поиск по проекту показывает только само определение в [`core/order_history.py:95`](core/order_history.py:95), внешних вызовов нет.

### 5. [`get_order_pairs()`](core/order_history.py:118) держит [`_orders_lock`](core/order_history.py:20) на всей FIFO-обработке
**Вердикт:** подтверждаю.

**Основание:** функция захватывает lock на [`core/order_history.py:135`](core/order_history.py:135) и освобождает только при возврате на [`core/order_history.py:221`](core/order_history.py:221). Внутри этой секции выполняется вся сортировка и полный цикл сопоставления ордеров, включая вложенный цикл [`while`](core/order_history.py:149). Это действительно блокирует параллельные вызовы вроде [`save_order()`](core/order_history.py:71).

### 6. Несоответствие семантики комиссии в бэктесте и live
**Вердикт:** подтверждаю.

**Основание:** live-путь хранит в ордере комиссию «за 1 лот, одна сторона» через [`make_order()`](core/order_history.py:29) и затем в [`get_order_pairs()`](core/order_history.py:118) считает round-trip как максимум из комиссий открытия/закрытия, умноженный на 2, на [`core/order_history.py:171-177`](core/order_history.py:171). Бэктест-путь в [`_close_trade()`](core/backtest_engine.py:216) отдельно считает вход и выход через [`commission_manager.calculate()`](core/backtest_engine.py:229) и суммирует их на [`core/backtest_engine.py:239`](core/backtest_engine.py:239). Это два разных алгоритма, поэтому утверждение о различии путей расчёта корректно.

---

## [`core/equity_tracker.py`](core/equity_tracker.py)

### 7. При [`position_qty=0`](core/equity_tracker.py:71) просадка не обновляется
**Вердикт:** подтверждаю.

**Основание:** просадка считается на [`core/equity_tracker.py:97-103`](core/equity_tracker.py:97) только внутри ветки [`if position_qty != 0`](core/equity_tracker.py:99). Если equity уже упал, но позиция закрыта и количество равно нулю, новая просадка не фиксируется.

### 8. [`reset()`](core/equity_tracker.py:141) — потенциально мёртвый код
**Вердикт:** подтверждаю.

**Основание:** поиск по проекту показывает только определение в [`core/equity_tracker.py:141`](core/equity_tracker.py:141), внешних вызовов нет.

---

## [`core/instrument_classifier.py`](core/instrument_classifier.py)

### 9. [`get_group()`](core/instrument_classifier.py:151), [`add_manual_mapping()`](core/instrument_classifier.py:164), [`add_prefix_rule()`](core/instrument_classifier.py:178), [`remove_manual_mapping()`](core/instrument_classifier.py:192), [`remove_prefix_rule()`](core/instrument_classifier.py:199) — мёртвый код
**Вердикт:** подтверждаю.

**Основание:** по проекту находятся только определения этих методов, внешних вызовов нет.

### 10. Хардкоженный относительный путь к конфигу
**Вердикт:** подтверждаю.

**Основание:** конструктор [`InstrumentClassifier.__init__()`](core/instrument_classifier.py:50) использует строку `data/commission_config.json` как значение по умолчанию и затем делает [`Path(config_path)`](core/instrument_classifier.py:57). Это путь относительно рабочей директории, а не относительно каталога проекта, поэтому замечание про возможную проблему в сборке корректно.

---

## [`core/commission_manager.py`](core/commission_manager.py)

### 11. [`load_config()`](core/commission_manager.py:331) и [`update_moex_rates()`](core/commission_manager.py:348) — мёртвый код
**Вердикт:** подтверждаю.

**Основание:** поиск по проекту показывает только определения; внешних вызовов нет.

### 12. Хардкоженный относительный путь к конфигу
**Вердикт:** подтверждаю.

**Основание:** конструктор [`CommissionManager.__init__()`](core/commission_manager.py:39) так же использует `data/commission_config.json`, а затем строит [`Path(config_path)`](core/commission_manager.py:46) без привязки к базовой директории приложения.

---

## [`core/moex_api.py`](core/moex_api.py)

### 13. [`_cache`](core/moex_api.py:30) — атрибут класса, общий для всех экземпляров
**Вердикт:** подтверждаю.

**Основание:** кэш объявлен на уровне класса на [`core/moex_api.py:29-31`](core/moex_api.py:29), а все основные методы помечены как [`@classmethod`](core/moex_api.py:43). Это действительно общий кэш для всех экземпляров класса.

### 14. Для акций [`point_cost`](core/moex_api.py:230) приравнен к шагу цены, что не совпадает с формулой PnL в live
**Вердикт:** подтверждаю.

**Основание:** в [`get_stock_info()`](core/moex_api.py:145) поле [`point_cost`](core/moex_api.py:230) устанавливается равным [`minstep`](core/moex_api.py:225). При этом live-движок считает PnL как разницу цен, умноженную на количество и на [`_point_cost`](core/live_engine.py:306). Для акций такая семантика действительно выглядит неверной: если цена уже выражена в рублях за бумагу, дополнительное умножение на шаг цены искажает результат.

---

## [`core/base_connector.py`](core/base_connector.py)

### 15. [`import math`](core/base_connector.py:4) — мёртвый импорт
**Вердикт:** подтверждаю.

**Основание:** в файле нет использования этого импорта. На расчёт backoff влияет встроенный [`min`](core/base_connector.py:180), а не модуль math.

### 16. Race condition в [`start_reconnect_loop()`](core/base_connector.py:142)
**Вердикт:** подтверждаю.

**Основание:** проверка существования и жизнеспособности [`_reconnect_thread`](core/base_connector.py:147) и создание нового потока на [`core/base_connector.py:149-152`](core/base_connector.py:149) не защищены отдельной синхронизацией. При параллельном входе два потока действительно могут одновременно пройти проверку и стартовать два reconnect-loop.

### 17. [`chase_order()`](core/base_connector.py:95) не реализован в [`QuikConnector`](core/quik_connector.py)
**Вердикт:** подтверждаю.

**Основание:** базовый класс бросает [`NotImplementedError`](core/base_connector.py:104), а в [`core/quik_connector.py`](core/quik_connector.py) переопределения нет. Значит, вызов через экземпляр QUIK-коннектора действительно упрётся в базовую реализацию.

---

## [`core/finam_connector.py`](core/finam_connector.py)

### 18. Критичная гонка: общие [`_candles_buffer`](core/finam_connector.py:49) и [`_candles_event`](core/finam_connector.py:48) для синхронной истории и подписки
**Вердикт:** подтверждаю.

**Основание:** те же самые поля используются и в [`get_history()`](core/finam_connector.py:1254), и в fallback [`_get_history_via_subscribe()`](core/finam_connector.py:1324), и в callback [`_on_candles()`](core/finam_connector.py:1208). Callback без разделения контекстов пишет в буфер на [`core/finam_connector.py:1238-1240`](core/finam_connector.py:1238). При одновременном использовании потоковой подписки и синхронного запроса пересечения неизбежны.

### 19. [`_get_history_via_subscribe()`](core/finam_connector.py:1324) обходит refcount-логику [`unsubscribe_candles()`](core/finam_connector.py:1185)
**Вердикт:** подтверждаю.

**Основание:** fallback напрямую отправляет XML-команду subscribe/unsubscribe на [`core/finam_connector.py:1333-1365`](core/finam_connector.py:1333), а не использует публичные методы [`subscribe_candles()`](core/finam_connector.py:1153) и [`unsubscribe_candles()`](core/finam_connector.py:1185), где и живёт учёт подписчиков.

### 20. [`get_last_price()`](core/finam_connector.py:1126) фактически всегда возвращает [`None`](core/finam_connector.py:1140)
**Вердикт:** подтверждаю.

**Основание:** метод отправляет [`get_securities_info`](core/finam_connector.py:1132), потом пытается синхронно прочитать [`last`](core/finam_connector.py:1139) из немедленного ответа. Комментарий в коде сам признаёт, что данные приходят через callback на [`core/finam_connector.py:1138`](core/finam_connector.py:1138). В такой схеме синхронный парсинг действительно почти наверняка даст пусто.

### 21. [`get_order_book()`](core/finam_connector.py:791) возвращает захардкоженный объём 1.0
**Вердикт:** подтверждаю.

**Основание:** заглушка формирует [`bids`](core/finam_connector.py:809) и [`asks`](core/finam_connector.py:810) с объёмом 1.0 вне зависимости от реальной ликвидности. Комментарий на [`core/finam_connector.py:807-808`](core/finam_connector.py:807) прямо говорит, что это условный объём.

---

## [`core/quik_connector.py`](core/quik_connector.py)

### 22. Критично: [`get_history()`](core/quik_connector.py:382) удерживает [`_lock`](core/quik_connector.py:25) на блокирующем вызове к QUIK
**Вердикт:** подтверждаю.

**Основание:** вызов [`get_candles_from_data_source`](core/quik_connector.py:398) выполняется внутри [`with self._lock`](core/quik_connector.py:397). Если QUIK зависнет, этот lock останется занят, и другие операции коннектора, тоже использующие его, будут ждать.

### 23. [`get_sec_info()`](core/quik_connector.py:512) не возвращает шаг цены и размер лота
**Вердикт:** подтверждаю.

**Основание:** метод наполняет словарь только полями [`buy_deposit`](core/quik_connector.py:548), [`sell_deposit`](core/quik_connector.py:549) и [`point_cost`](core/quik_connector.py:558). Ни [`minstep`](core/quik_connector.py:569), ни размер лота из данных MOEX не кладутся в результат, хотя для динамического лота UI читает [`lotsize`](core/live_engine.py:766).

---

## [`core/live_engine.py`](core/live_engine.py)

### 24. Использование [`getattr(self._connector, '_connector_id', 'finam')`](core/live_engine.py:593) даёт неверный выбор таймаута
**Вердикт:** подтверждаю.

**Основание:** в самом [`LiveEngine.__init__()`](core/live_engine.py:60) идентификатор коннектора уже вычисляется и сохраняется в [`self._connector_id`](core/live_engine.py:79). Но в [`_load_and_update()`](core/live_engine.py:562) код вместо этого читает атрибут с самого объекта коннектора на [`core/live_engine.py:593`](core/live_engine.py:593). У [`FinamConnector`](core/finam_connector.py:23) и [`QuikConnector`](core/quik_connector.py:14) такого атрибута нет, поэтому фактически всегда берётся значение по умолчанию `finam`, а QUIK не получает свой увеличенный таймаут.

---

## [`core/backtest_engine.py`](core/backtest_engine.py)

### 25. [`point_cost`](core/backtest_engine.py:153) берётся из атрибута модуля стратегии и практически всегда остаётся 1.0
**Вердикт:** подтверждаю.

**Основание:** при открытии сделки используется [`getattr(module, 'point_cost', 1.0)`](core/backtest_engine.py:153). Поиск по стратегиям не находит определений такого атрибута на уровне модуля, значит путь по умолчанию действительно срабатывает всегда.

### 26. Для фьючерсных файлов [`bars[0].board`](core/backtest_engine.py:143) остаётся `TQBR`
**Вердикт:** подтверждаю.

**Основание:** бэктест загружает данные через [`self._loader.load(filepath)`](core/backtest_engine.py:86) без передачи board. А [`TXTLoader.load()`](core/txt_loader.py:35) по умолчанию использует board=`TQBR`, и это значение записывается в бар на [`core/txt_loader.py:95`](core/txt_loader.py:95). Следовательно, если board отдельно не передан в загрузчик, у фьючерсного файла действительно останется фондовый дефолт.

---

## [`core/strategy_loader.py`](core/strategy_loader.py)

### 27. [`on_tick`](core/strategy_loader.py:12) обязателен, но в текущей архитектуре не используется
**Вердикт:** подтверждаю.

**Основание:** обязательность зафиксирована в [`REQUIRED_FUNCTIONS`](core/strategy_loader.py:12). Метод-обёртка [`call_on_tick()`](core/strategy_loader.py:89) существует, но поиск по проекту не показывает его вызовов из runtime-компонентов, включая live-движок.

---

## [`core/autostart.py`](core/autostart.py)

### 28. [`stop_live_engine()`](core/autostart.py:67) блокирует GUI
**Вердикт:** частично подтверждаю.

**Что подтверждаю:** вызов действительно синхронный и вызывает [`engine.stop()`](core/autostart.py:85), а внутри [`LiveEngine.stop()`](core/live_engine.py:410) есть ожидание завершения chase-ордеров на [`core/live_engine.py:423-426`](core/live_engine.py:423) и join основного потока на [`core/live_engine.py:433-434`](core/live_engine.py:433). Если это вызывается из GUI-потока, интерфейс может подвиснуть.

**Что не подтверждаю:** ожидание происходит не внутри [`with _live_engines_lock`](core/autostart.py:77), потому что lock освобождается после удаления записи на [`core/autostart.py:82`](core/autostart.py:82), а вызов [`engine.stop()`](core/autostart.py:85) расположен уже вне критической секции.

---

## [`core/telegram_bot.py`](core/telegram_bot.py)

### 29. [`EventCode.ORDER_PLACED`](core/telegram_bot.py:32) и [`EventCode.ORDER_FILLED`](core/telegram_bot.py:33) отсутствуют в перечислении и шаблонах
**Вердикт:** не подтверждаю.

**Основание:** оба кода событий объявлены в [`core/telegram_bot.py:32-33`](core/telegram_bot.py:32), а шаблоны для них присутствуют на [`core/telegram_bot.py:99-108`](core/telegram_bot.py:99). Следовательно, описанный дефект в текущем состоянии кода отсутствует.

---

## [`strategies/bochka_cny.py`](strategies/bochka_cny.py)

### 30. [`execute_signal()`](strategies/bochka_cny.py:222) не пишет историю ордеров
**Вердикт:** подтверждаю.

**Основание:** в файле нет вызовов логики, аналогичной [`_record_trade()`](strategies/achilles.py:266), а также нет обращений к [`save_order()`](core/order_history.py:71) или [`append_trade()`](core/storage.py:197). Заявки реально отправляются через коннектор, но запись в историю в самой стратегии отсутствует.

### 31. Вызов [`place_order()`](strategies/bochka_cny.py:381) передаёт несуществующий именованный аргумент
**Вердикт:** подтверждаю.

**Основание:** рыночный вызов на [`strategies/bochka_cny.py:381-390`](strategies/bochka_cny.py:381) передаёт [`comment=comment`](strategies/bochka_cny.py:389), а сигнатура [`place_order()`](core/base_connector.py:36) такого параметра не содержит. Для коннекторов, следующих базовому интерфейсу, это действительно приведёт к ошибке вызова.

---

## [`strategies/tracker.py`](strategies/tracker.py)

### 32. «Реверс не работает в live» в сформулированном виде
**Вердикт:** не подтверждаю.

**Основание:** текущая версия [`on_bar()`](strategies/tracker.py:164) не пытается сделать мгновенный реверс сигналом вида buy/sell с удвоенным объёмом. Напротив, при противоположном сигнале она сначала отдаёт только [`close`](strategies/tracker.py:208) или [`close`](strategies/tracker.py:218), а новый вход предполагается на следующем баре. Поэтому описанная в пункте проблема не соответствует текущему коду.

---

## [`strategies/valera_trend.py`](strategies/valera_trend.py)

### 33. Нет fallback-закрытия при пропуске бара с [`time_close`](strategies/valera_trend.py:141)
**Вердикт:** не подтверждаю.

**Основание:** в текущей версии [`on_bar()`](strategies/valera_trend.py:119) закрытие выполняется не по строгому равенству, а по диапазону [`time_close <= time_min < time_open`](strategies/valera_trend.py:159). Это как раз и является fallback-механизмом при пропуске точного бара времени закрытия.

---

## [`ui/param_widgets.py`](ui/param_widgets.py)

### 34. В [`CommissionParamWidget`](ui/param_widgets.py:613) установлено 10 знаков после запятой
**Вердикт:** подтверждаю.

**Основание:** оба спинбокса задают [`setDecimals(10)`](ui/param_widgets.py:627) и [`setDecimals(10)`](ui/param_widgets.py:635). Техническая часть замечания полностью верна; оценка «неудобно» относится уже к UX, но предпосылка действительно есть.

---

## [`ui/strategy_window.py`](ui/strategy_window.py)

### 35. [`_refresh_lot_preview()`](ui/strategy_window.py:723) может блокировать GUI для QUIK
**Вердикт:** подтверждаю.

**Основание:** метод вызывается таймером из GUI-потока на [`ui/strategy_window.py:713-714`](ui/strategy_window.py:713) и внутри синхронно вызывает [`connector.get_free_money()`](ui/strategy_window.py:738) и [`connector.get_sec_info()`](ui/strategy_window.py:745). Для QUIK эти методы делают синхронные обращения под [`_lock`](core/quik_connector.py:470), [`_lock`](core/quik_connector.py:551), [`_lock`](core/quik_connector.py:564) и [`_lock`](core/quik_connector.py:569). Если QUIK тормозит, UI действительно будет ждать.

---

## [`ui/main_window.py`](ui/main_window.py)

### 36. [`_stop_agent()`](ui/main_window.py:1352) блокирует GUI
**Вердикт:** подтверждаю.

**Основание:** метод напрямую вызывает [`stop_live_engine()`](ui/main_window.py:1354) на [`ui/main_window.py:1355`](ui/main_window.py:1355), без вынесения в рабочий поток. А внутри остановки есть синхронные ожидания, описанные выше. Поэтому интерфейс действительно может подвисать на время остановки.

---

## [`ui/backtest_window.py`](ui/backtest_window.py)

### 37. Monkey-patch [`get_params()`](ui/backtest_window.py:424) накапливается между повторными запусками
**Вердикт:** подтверждаю.

**Основание:** в [`_build_strategy()`](ui/backtest_window.py:418) берётся результат текущего [`self._strategy_module.get_params()`](ui/backtest_window.py:425), затем изменяются [`default`](ui/backtest_window.py:428) и сам метод переопределяется лямбдой на [`ui/backtest_window.py:429`](ui/backtest_window.py:429). При повторном запуске читается уже подменённая версия, то есть дефолты действительно наслаиваются.

---

## [`core/moex_commission_fetcher.py`](core/moex_commission_fetcher.py)

### 38. Класс-заглушка и warning «при каждом открытии вкладки»
**Вердикт:** не подтверждаю в заявленной формулировке.

**Что подтверждаю:** [`_fetch_from_moex()`](core/moex_commission_fetcher.py:62) действительно является заглушкой и всегда возвращает [`None`](core/moex_commission_fetcher.py:90). Также по проекту нет вызовов [`fetch_rates()`](core/moex_commission_fetcher.py:33), так что автоматическое создание кэша в текущем коде не происходит.

**Что не подтверждаю:** warning при каждом открытии вкладки не следует напрямую из текущей реализации. При открытии виджета вызывается [`_check_rates_freshness()`](ui/commission_settings_widget.py:528), но если кэш отсутствует, функция выходит ранним возвратом на [`ui/commission_settings_widget.py:532-535`](ui/commission_settings_widget.py:532) без предупреждающего диалога. То есть часть про «warning при каждом открытии вкладки» в текущем коде неверна.

---

## Итог

По состоянию текущего репозитория большинство пунктов со статусом «ПРАВДА» действительно подтверждаются исходным кодом. Исключения составляют:

- [`core/telegram_bot.py`](core/telegram_bot.py) — пункт не подтверждён;
- [`strategies/tracker.py`](strategies/tracker.py) — пункт не подтверждён;
- [`strategies/valera_trend.py`](strategies/valera_trend.py) — пункт не подтверждён;
- [`core/moex_commission_fetcher.py`](core/moex_commission_fetcher.py) — подтверждена только часть про заглушку, но не часть про warning при каждом открытии вкладки;
- [`core/autostart.py`](core/autostart.py) — подтверждена блокировка GUI, но не удержание [`_live_engines_lock`](core/autostart.py:58) во время ожидания остановки.
