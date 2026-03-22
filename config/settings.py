import sys
from pathlib import Path

# В PyInstaller-сборке (_MEIPASS) __file__ указывает на временную папку.
# Для данных (data/, logs/, strategies/) используем папку рядом с exe.
# В режиме разработки — папку проекта (parent.parent от config/).
if getattr(sys, 'frozen', False):
    # Запущен как exe — папка рядом с TradingManager.exe
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

APP_PROFILE_DIR = BASE_DIR / "app_profile"
APP_PROFILE_DIR.mkdir(exist_ok=True)

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

STRATEGIES_DIR = BASE_DIR / "strategies"
STRATEGIES_DIR.mkdir(exist_ok=True)

APP_NAME = "Trading Strategy Manager"
APP_VERSION = "1.0.0"

# Интервал обновления позиций в секундах
POSITIONS_REFRESH_INTERVAL = 5

# Время окончания торгов (в минутах от начала дня). 1425 = 23:45
# Используется для автоматического снятия лимитных ордеров перед клирингом
TRADING_END_TIME_MIN = 1425

# Комиссии брокера для расчета PnL
# Комиссия за лот фьючерса в рублях (абсолютное значение за открытие/закрытие позиции)
COMMISSION_FUTURES = 2.0

# Комиссия за сделку с акциями в процентах от суммы сделки (0.05 = 0.05%)
COMMISSION_STOCK = 0.05

class NotificationLevel:
    ALL = "all"
    ERRORS_ONLY = "errors"
    CRITICAL_ONLY = "critical"

DEFAULT_NOTIFICATION_LEVEL = NotificationLevel.ALL
