# Документация коннекторов OsEngine

## Обзор архитектуры

Все коннекторы в OsEngine следуют единому паттерну:

```
AServer (абстрактный базовый класс)
    └── ServerRealization : IServerRealization (конкретная реализация)
```

- **AServer** — обёртка, хранит параметры сервера и делегирует всю работу `ServerRealization`
- **IServerRealization** — интерфейс, определяющий контракт коннектора

---

## 1. Transaq коннектор

### Файлы
| Файл | Назначение |
|------|-----------|
| `TransaqServer.cs` | Основной файл: классы `TransaqServer` и `TransaqServerRealization` |
| `TransaqServerPermission.cs` | Права коннектора (возвращает `ServerType.Transaq`) |
| `ChangeTransaqPassword.xaml/.cs` | UI окно смены пароля |
| `TransaqEntity/TransaqEntities.cs` | XML-сущности: Security, Order, Trade, Quote, Candle, News и др. |
| `TransaqEntity/TransaqPortfolio.cs` | XML-сущности портфеля: McPortfolio, Money, Portfolio_currency |
| `TransaqEntity/TransaqPositions.cs` | XML-сущности позиций: TransaqPositions, Forts_position, ClientLimits |
| `TransaqEntity/InfoActiveOrder.cs` | Вспомогательный класс для отслеживания активных ордеров |

### Внешние зависимости
- **Нативная DLL**: `txmlconnector64.dll` (Transaq XML Connector)
- **NuGet**: `RestSharp` (для XML десериализации)

### Подключение (Connect)
```
1. Проверка времени подключения (параметр "6:50/23:50" — московское время)
2. ConnectorInitialize() → вызов Initialize() из txmlconnector64.dll
3. SetCallback() → регистрация callback-делегата для получения данных
4. Формирование XML-команды <command id="connect">:
   <login>, <password>, <host>, <port>, <milliseconds>, <push_pos_equity>, <rqdelay>
5. ConnectorSendCommand(cmd) → отправка команды в нативную библиотеку
```

### Параметры подключения
| Индекс | Параметр | По умолчанию | Описание |
|--------|----------|-------------|----------|
| 0 | Login | "" | Логин |
| 1 | Password | "" | Пароль |
| 2 | Host | "tr1.finam.ru" | Адрес сервера |
| 3 | Port | "3900" | Порт |
| 4 | WorkingTime | "6:50/23:50" | Время работы (московское) |
| 5 | UseMoexStock | true | Фондовый рынок MOEX |
| 6 | UseFunds | false | Паевые фонды |
| 7 | UseOtcStock | false | Внебиржевые инструменты |
| 8 | UseFutures | true | Фьючерсы |
| 9 | UseCurrency | false | Валюта |
| 10 | UseOptions | false | Опционы |
| 11 | UseOther | false | Прочее |
| 12 | FullLogConnector | false | Полное логирование |
| 13 | Button | — | Кнопка смены пароля |
| 14 | ReconnectingAfterNone | true | Переподключение при неизвестном статусе |
| 22 | FullMarketDepth | — | Полная стакан (true) или BestBidAsk (false) |

### Команды (XML-протокол)

#### Подписка на инструмент (Subscribe)
```xml
<command id="subscribe">
    <alltrades>
        <security><board>FUT</board><seccode>SiZ4</seccode></security>
    </alltrades>
    <quotes>  <!-- или <quotations> для BestBidAsk -->
        <security><board>FUT</board><seccode>SiZ4</seccode></security>
    </quotes>
</command>
```

#### Выставление ордера (SendOrder)
```xml
<command id="neworder">
    <security><board>FUT</board><seccode>SiZ4</seccode></security>
    <client>SPBFUT00000</client>
    <price>100.50</price>  <!-- или <bymarket/> для рыночного -->
    <quantity>1</quantity>
    <buysell>B</buysell>   <!-- B=Buy, S=Sell -->
    <brokerref>12345</brokerref>  <!-- NumberUser ордера -->
    <unfilled>PutInQueue</unfilled>
</command>
```

#### Отмена ордера (CancelOrder)
```xml
<command id="cancelorder">
    <transactionid>12345</transactionid>
</command>
```

#### Изменение цены ордера (ChangeOrderPrice)
```xml
<command id="moveorder">
    <transactionid>12345</transactionid>
    <price>101.00</price>
    <moveflag>0</moveflag>
</command>
```

#### Запрос свечей (GetCandleData)
```xml
<command id="get_history">
    <security><board>FUT</board><seccode>SiZ4</seccode></security>
    <period>1</period>  <!-- 1=Min1, 2=Min5, 3=Min15, 4=Hour1, 5=Day -->
    <count>100</count>
</command>
```

### Входящие данные (Callback)
Все данные приходят через callback `CallBackDataHandler(IntPtr pData)`:

| XML-тег | Обработка |
|---------|-----------|
| `<securities>` | Создание инструментов (Security) |
| `<quotes>` | Обновление стакана (MarketDepth) |
| `<quotations>` | BestBidAsk (лучшие котировки) |
| `<alltrades>` | Лента сделок (Trade) |
| `<orders>` | Обновление ордеров (Order) |
| `<trades>` | Мои сделки (MyTrade) |
| `<mc_portfolio>` | Портфели (Portfolio) |
| `<positions>` | Позиции |
| `<clientlimits>` | Лимиты клиента |
| `<candles>` | Исторические свечи |
| `<news_header>` | Заголовок новости |
| `<news_body>` | Текст новости |
| `<server_status>` | Статус соединения |
| `<error>` | Ошибка от сервера |

### Фоновые потоки (8 штук)
| Поток | Назначение |
|-------|-----------|
| ThreadTransaqGetPortfolio | Периодический запрос портфелей |
| ThreadTransaqDataParsing | Разбор входящих данных |
| TransaqThreadTradesParsing | Обработка ленты сделок |
| TransaqThreadDepthsParsing | Обработка стакана |
| TransaqThreadConverter | Маршрутизация сообщений по очередям |
| TransaqThreadUpdateSecurity | Обновление инструментов и подписка |
| TransaqThreadUpdateHistoricalData | Загрузка исторических данных |
| TransaqThreadUpdateSecurityInfo | Запрос информации об инструментах |

### Нативные функции (DllImport)
```csharp
[DllImport("txmlconnector64.dll")]
static extern bool SetCallback(CallBackDelegate pCallback);

[DllImport("txmlconnector64.dll")]
static extern IntPtr SendCommand(IntPtr pData);

[DllImport("txmlconnector64.dll")]
static extern bool FreeMemory(IntPtr pData);

[DllImport("TXmlConnector64.dll")]
static extern IntPtr Initialize(IntPtr pPath, Int32 logLevel);

[DllImport("TXmlConnector64.dll")]
static extern IntPtr UnInitialize();

[DllImport("TXmlConnector64.dll")]
static extern IntPtr SetLogLevel(Int32 logLevel);
```

---

## 2. QuikLua коннектор

### Файлы
| Файл | Назначение |
|------|-----------|
| `QuikLuaServer.cs` | Основной файл: классы `QuikLuaServer` и `QuikLuaServerRealization` |
| `QuikLuaServerPermission.cs` | Права коннектора (возвращает `ServerType.QuikLua`) |
| `Entity/CustomTraceListener.cs` | Кастомный TraceListener для перехвата ошибок QuikSharp |

### Внешние зависимости
- **NuGet**: `QuikSharp` (библиотека для работы с QUIK через Lua)
- **NuGet**: `Newtonsoft.Json` (сериализация)

### Подключение (Connect)
```
1. Чтение параметров из настроек
2. Создание QuikLua = new Quik(port, new InMemoryStorage())
3. Подписка на события QuikLua.Events:
   OnConnected, OnDisconnected, OnConnectedToQuik, OnDisconnectedFromQuik,
   OnClose, OnDepoLimit, OnMoneyLimit, OnTrade, OnOrder, OnAllTrade,
   OnQuote, OnFuturesClientHolding, OnFuturesLimitChange, OnTransReply
4. QuikLua.Service.QuikService.Start() — запуск сервиса
5. Проверка подключения: QuikLua.Service.IsConnected().Result
6. Загрузка клиентских кодов и торговых счетов
```

### Параметры подключения
| Индекс | Параметр | По умолчанию | Описание |
|--------|----------|-------------|----------|
| 0 | UseStock | true | Акции |
| 1 | UseFutures | true | Фьючерсы |
| 2 | UseCurrency | true | Валюта |
| 3 | UseOptions | false | Опционы |
| 4 | UseBonds | false | Облигации |
| 5 | UseOther | false | Прочее |
| 6 | Label109 | false | Режим (доп. настройка) |
| 7 | Client code | null | Код клиента (опционально) |
| 8 | TradeMode | "T0" | Режим торгов (T0/T1/T2/NotImplemented) |
| 9 | FullLogConnector | false | Полное логирование |
| 10 | Port | 34130 | Порт для QuikSharp |
| 11 | PortfolioOnlyBots | false | Только портфели ботов |
| 12 | PortfolioSeparator | "/" | Разделитель портфеля |

### События QuikSharp → OsEngine
| Событие | Назначение |
|---------|-----------|
| OnConnected | Подключение к сервису QuikSharp |
| OnDisconnected | Отключение от сервиса |
| OnConnectedToQuik | Подключение к терминалу QUIK |
| OnDisconnectedFromQuik | Отключение от терминала QUIK |
| OnOrder | Новый/обновлённый ордер |
| OnTrade | Моя сделка |
| OnAllTrade | Общая лента сделок |
| OnQuote | Обновление стакана |
| OnDepoLimit | Лимит по бумагам (спот) |
| OnMoneyLimit | Денежный лимит |
| OnFuturesClientHolding | Позиции по фьючерсам |
| OnFuturesLimitChange | Изменение лимитов фьючерсов |
| OnTransReply | Ответ на транзакцию |

### Методы работы с данными
| Метод | Описание |
|-------|----------|
| `QuikLua.Class.GetClassesList()` | Получить список классов |
| `QuikLua.Class.GetClassSecurities(classCode)` | Получить инструменты класса |
| `QuikLua.Class.GetSecurityInfo(classCode, secCode)` | Информация об инструменте |
| `QuikLua.Trading.GetParamEx(...)` | Получить параметр инструмента |
| `QuikLua.Candles.GetAllCandles(...)` | Получить все свечи |
| `QuikLua.Candles.GetLastCandles(...)` | Получить последние свечи |
| `QuikLua.Orders.CreateOrder(...)` | Выставить ордер |
| `QuikLua.Orders.KillOrder(...)` | Отменить ордер |
| `QuikLua.Orders.GetOrder(...)` | Получить ордер |
| `QuikLua.Orders.GetOrders()` | Получить все ордера |
| `QuikLua.Trading.GetFuturesClientLimits()` | Лимиты фьючерсов |
| `QuikLua.Trading.GetDepoLimits()` | Лимиты по бумагам |
| `QuikLua.Trading.GetMoneyLimits()` | Денежные лимиты |
| `QuikLua.OrderBook.Subscribe(...)` | Подписка на стакан |

### Фоновые потоки (5 штук)
| Поток | Назначение |
|-------|-----------|
| QuikLuaGetPortfoliosArea | Периодическое обновление портфелей |
| QuikLuaThreadTradesParsingWorkPlace | Обработка ленты сделок |
| QuikLuaThreadMarketDepthsParsingWorkPlace | Обработка стакана |
| QuikLuaThreadDataParsingWorkPlace | Обработка ордеров и сделок |
| QuikLuaThreadPing | Ping-мониторинг (проверка живости потоков) |

### Подписка на стакан
```csharp
QuikLua.OrderBook.Subscribe(security.NameClass, security.Name.Split('+')[0]);
```

### Выставление ордера
```csharp
QuikSharp.DataStructures.Transaction.Order qOrder = new();
qOrder.SecCode = order.SecurityNameCode.Split('+')[0];
qOrder.Account = order.PortfolioNumber.Split(_portfolioSeparator)[0];
qOrder.ClientCode = order.PortfolioNumber.Split(_portfolioSeparator)[1];
qOrder.ClassCode = security.NameClass;
qOrder.Quantity = Convert.ToInt32(order.Volume);
qOrder.Operation = order.Side == Side.Buy ? Operation.Buy : Operation.Sell;
qOrder.Price = order.Price;

long res = QuikLua.Orders.CreateOrder(qOrder).Result;
```

---

## 3. Общие правила работы коннекторов

### Жизненный цикл
```
1. Создание AServer → создание ServerRealization
2. Загрузка параметров из файла (Engine/{ServerName}Params.txt)
3. Вызов Connect(proxy) → подключение
4. ConnectEvent → уведомление об успешном подключении
5. GetSecurities() → загрузка инструментов
6. SecurityEvent → инструменты загружены
7. GetPortfolios() → загрузка портфелей
8. PortfolioEvent → портфели загружены
9. Subscribe(security) → подписка на инструмент
10. MarketDepthEvent / NewTradesEvent → данные по подписке
11. SendOrder(order) → выставление ордера
12. MyOrderEvent / MyTradeEvent → ответы по ордеру
13. Dispose() → отключение и освобождение ресурсов
14. DisconnectEvent → уведомление об отключении
```

### События IServerRealization
| Событие | Когда вызывается |
|---------|-----------------|
| ConnectEvent | Успешное подключение |
| DisconnectEvent | Отключение (любая причина) |
| SecurityEvent | Загружен список инструментов |
| PortfolioEvent | Обновление портфелей |
| MarketDepthEvent | Обновление стакана |
| NewTradesEvent | Обновление ленты сделок |
| MyOrderEvent | Обновление моего ордера |
| MyTradeEvent | Новая моя сделка |
| NewsEvent | Новая новость |
| LogMessageEvent | Лог-сообщение |
| ForceCheckOrdersAfterReconnectEvent | Требуется перепроверка ордеров |

### Rate Limiting
Оба коннектора используют `RateGate` для ограничения частоты запросов:
- **Transaq**: Subscribe — 1 запрос / 300ms, ChangeOrderPrice — 1 запрос / 200ms
- **QuikLua**: SendOrder — настраиваемый rate limit

### Переподключение
- **Transaq**: При статусе "inactive" или потере связи — автоматическое переподключение
- **QuikLua**: Ping-поток проверяет живость всех потоков каждые 30 секунд

---

## 3. Расчёт PnL и учёт комиссии (Transaq)

### 3.1. Как рассчитывается PnL

OsEngine **НЕ рассчитывает** PnL самостоятельно для коннектора Transaq. Брокерский сервер Transaq сам рассчитывает нереализованный PnL и присылает его в готовом виде. OsEngine только **парсит** и отображает эти данные.

#### Источники PnL от брокера

| Поле Portfolio | Источник XML | Что означает |
|----------------|-------------|--------------|
| `ValueBegin` | `<open_equity>` / `<money_current>` | Стоимость на начало сессии |
| `ValueCurrent` | `<equity>` / `<money_free>` | Текущая стоимость |
| `ValueBlocked` | `(equity - cover) + go` / `<money_reserve>` | Заблокировано в ордерах |
| **`UnrealizedPnl`** | **`<unrealized_pnl>` / `<profit>`** | **Нереализованный PnL** |

#### Запрос данных у брокера

**1. Подписка на push-уведомления при подключении:**
```xml
<command id="connect">
    ...
    <push_pos_equity>3</push_pos_equity>  <!-- частота push портфеля: каждые 3 сек -->
    ...
</command>
```

**2. Периодический запрос портфелей (каждые 3 секунды) — поток `CycleGettingPortfolios()`:**

Для MCT-клиентов:
```xml
<command id="get_portfolio_mct" client="SPBFUT00000"/>
```

Для MC-клиентов (union или client):
```xml
<command id="get_mc_portfolio" union="FZ12345"/>
<!-- или -->
<command id="get_mc_portfolio" client="SPBFUT00000"/>
```

Для фьючерсных лимитов:
```xml
<command id="get_client_limits" client="SPBFUT00000"/>
```

#### Парсинг ответа брокера (ParsePortfolio)

```csharp
// PnL портфеля — БРОКЕР уже рассчитал
portfolio.UnrealizedPnl = pnl.InnerText.ToDecimal();  // из <unrealized_pnl>

// Стоимость портфеля
portfolio.ValueBegin = openEquity.InnerText.ToDecimal();   // на начало дня
portfolio.ValueCurrent = equity.InnerText.ToDecimal();     // текущая

// Заблокированные средства = (equity - cover) + go
portfolio.ValueBlocked = (equity - cover) + go;
```

#### PnL по каждой позиции

```csharp
// PnL по конкретной бумаге — тоже от брокера
pos.UnrealizedPnl = pnlPos.InnerText.ToDecimal();  // из <security>/<unrealized_pnl>

// Текущая позиция = начальная + куплено - продано
pos.ValueCurrent = pos.ValueBegin + bought/lot - sold/lot;
```

#### PnL из лимитов клиента (InitPortfolio)

```csharp
portfolio.ValueBegin = clientLimits.MoneyCurrent.ToDecimal();
portfolio.ValueCurrent = clientLimits.MoneyFree.ToDecimal();
portfolio.ValueBlocked = clientLimits.MoneyReserve.ToDecimal();
portfolio.UnrealizedPnl = clientLimits.Profit.ToDecimal();  // PnL из <profit>
```

#### Частота обновления

- **Push-уведомления**: каждые 3 секунды (настроено через `<push_pos_equity>3</push_pos_equity>`)
- **Циклический запрос**: каждые 3 секунды в потоке `CycleGettingPortfolios()`
- **Событие**: `PortfolioEvent?.Invoke(_portfolios)` — уведомление UI/роботов

---

### 3.2. Учёт комиссии

#### Краткий ответ

**OsEngine НЕ учитывает комиссию от брокера при расчёте PnL для коннектора Transaq.**

Комиссия приходит от брокера в готовых данных портфеля, но OsEngine **не парсит** и **не использует** поля комиссии из ответов Transaq.

#### Какие поля комиссии приходят от брокера (но НЕ используются)

| XML-поле | Сущность | Где определено | Используется? |
|----------|---------|----------------|---------------|
| `<comission>` в `<trade>` | `Trade.Comission` | TransaqEntities.cs:468 | ❌ Нет |
| `<comission>` в `<money_position>` | `Money_position.Comission` | TransaqPositions.cs:40 | ❌ Нет |
| `<fee>` в `<money>` | `Money.Fee` | TransaqPortfolio.cs:157 | ❌ Нет |
| `<exchange_fee>` в `<clientlimits>` | `ClientLimits.ExchangeFee` | TransaqPositions.cs:214 | ❌ Нет |
| `<maxcomission>` в `<order>` | `Order.Maxcomission` | TransaqEntities.cs:435 | ❌ Нет |

#### Пример: UpdateMyTrades() НЕ парсит комиссию

```csharp
private void UpdateMyTrades(List<TransaqEntity.Trade> trades)
{
    for (int i = 0; i < trades.Count; i++)
    {
        TransaqEntity.Trade trade = trades[i];

        MyTrade myTrade = new MyTrade();
        myTrade.Time = DateTime.Parse(trade.Time);
        myTrade.NumberOrderParent = trade.Orderno;
        myTrade.NumberTrade = trade.Tradeno;
        myTrade.Volume = trade.Quantity.ToDecimal();
        myTrade.Price = trade.Price.ToDecimal();
        myTrade.SecurityNameCode = trade.Seccode;
        myTrade.Side = trade.Buysell == "B" ? Side.Buy : Side.Sell;
        // trade.Comission — НЕ ПАРСИТСЯ!
        
        MyTradeEvent?.Invoke(myTrade);
    }
}
```

#### Как OsEngine учитывает комиссию (в других местах)

Комиссия в OsEngine **настраивается вручную** на уровне робота/журнала, а не берётся от брокера:

**Типы комиссии (CommissionType):**
```csharp
public enum CommissionType
{
    None,           // Без комиссии
    OneLotFix,      // Фиксированная за 1 лот
    Percent         // Процент от оборота
}
```

**Где применяется:**
- **BotTabScreener** — настройки `CommissionType` и `CommissionValue`
- **Optimizer** — параметры `_commissionType` и `_commissionValue`
- **Journal** — журнал сделок применяет комиссию при расчёте PnL

**Как рассчитывается:**
```csharp
// Из BotTabPolygon.cs
if (CommissionType == CommissionPolygonType.Percent && CommissionValue != 0)
{
    result = result - result * (CommissionValue / 100);
}
```

#### Итоговая картина PnL

| Источник PnL | Учитывается? | Откуда берётся |
|--------------|-------------|----------------|
| `<unrealized_pnl>` портфеля | ✅ Да | Брокер рассчитал, OsEngine парсит |
| `<profit>` из clientlimits | ✅ Да | Брокер рассчитал, OsEngine парсит |
| `<unrealized_pnl>` по позиции | ✅ Да | Брокер рассчитал, OsEngine парсит |
| `<comission>` из trade | ❌ Нет | Приходит, но игнорируется |
| `<fee>` из money | ❌ Нет | Приходит, но игнорируется |
| `<exchange_fee>` из clientlimits | ❌ Нет | Приходит, но игнорируется |
| Ручная комиссия (CommissionType) | ✅ Да | Настраивается в роботе/журнале |

#### Вывод

**PnL в OsEngine для Transaq = PnL от брокера (unrealized_pnl) БЕЗ вычета комиссий.**

Реальный PnL (с учётом комиссий) будет **ниже** чем показывает OsEngine. Разница = сумма всех комиссий за сделки, которые OsEngine не вычитает.

**Если нужен точный PnL с комиссиями:**
1. Настроить `CommissionType` и `CommissionValue` в настройках робота
2. Или доработать `UpdateMyTrades()` для парсинга `<comission>` и вычета из PnL

---

## 4. Что НЕ входит в скопированные файлы

Для полной работы коннекторов также требуются (но НЕ скопированы):

### Общие файлы (не скопированы)
| Файл | Зачем нужен |
|------|------------|
| `AServer.cs` | Базовый класс для всех серверов |
| `IServerRealization.cs` | Интерфейс реализации сервера |
| `Entity/ServerParameter.cs` | Классы параметров сервера |
| `Entity/RateGate.cs` | Rate limiting |
| `Entity/TimeManager.cs` | Управление временем |
| `ServerMaster.cs` | Фабрика серверов (создание экземпляров) |
| `Entity/Order.cs`, `Entity/Security.cs`, `Entity/Trade.cs` | Основные сущности OsEngine |
| `Entity/Portfolio.cs`, `Entity/MarketDepth.cs` | Сущности портфеля и стакана |
| `Entity/Candle.cs` | Сущность свечи |
| `Entity/MyTrade.cs` | Сущность "моя сделка" |
| `Entity/News.cs` | Сущность новости |
| `Logging/LogMessageType.cs` | Типы лог-сообщений |
| `Language/OsLocalization.cs` | Локализация |
| `Language/TraderLocal.cs` | Переводы для трейдера |
| `Language/MarketLocal.cs` | Переводы для рынка |

### Внешние библиотеки (NuGet)
- **Transaq**: `RestSharp`, нативная `txmlconnector64.dll`
- **QuikLua**: `QuikSharp`, `Newtonsoft.Json`

### Для QuikLua
- Запущенный терминал QUIK с настроенным Lua-сервером
- Порт по умолчанию: 34130

### Для Transaq
- Нативная библиотека `txmlconnector64.dll` (не входит в проект)
- Доступ к серверу Transaq (по умолчанию tr1.finam.ru:3900)
