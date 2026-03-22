# core/txt_loader.py

import csv
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class Bar:
    ticker: str
    dt: datetime          # полная дата + время
    date_int: int         # YYYYMMDD как int (для фильтров стратегии)
    time_min: int         # минуты от полуночи (для фильтров стратегии)
    weekday: int          # 1=Пн ... 7=Вс (TSLab-совместимо)
    open: float
    high: float
    low: float
    close: float
    vol: int


class TXTLoader:
    """
    Парсинг FINAM-файлов формата:
    <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>
    """

    # Маппинг weekday(): 0=Mon..6=Sun → 1=Mon..7=Sun (как в TSLab)
    _WEEKDAY_MAP = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7}

    def load(self, filepath: str | Path) -> list[Bar]:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")

        bars: list[Bar] = []

        with path.open(encoding="utf-8") as f:
            reader = csv.reader(f)
            for line_num, row in enumerate(reader, start=1):
                # Пропускаем заголовок
                if line_num == 1 and row[0].startswith("<"):
                    logger.debug(f"Заголовок пропущен: {row}")
                    continue

                bar = self._parse_row(row, line_num)
                if bar is not None:
                    bars.append(bar)

        if not bars:
            raise ValueError(f"Файл пустой или не удалось распарсить строки: {path}")

        logger.info(
            f"Загружено {len(bars)} баров | {bars[0].ticker} | "
            f"{bars[0].dt.date()} → {bars[-1].dt.date()}"
        )
        return bars

    def _parse_row(self, row: list[str], line_num: int) -> Bar | None:
        try:
            ticker   = row[0].strip()
            # row[1]  = период (игнорируем)
            date_str = row[2].strip()   # YYYYMMDD
            time_str = row[3].strip()   # HHMMSS или HHMM

            open_  = float(row[4])
            high   = float(row[5])
            low    = float(row[6])
            close  = float(row[7])
            vol    = int(row[8])

            dt = self._parse_dt(date_str, time_str)
            date_int = int(date_str[2:])        # YYMMDD (TSLab-совместимо: 220225)
            time_min = dt.hour * 60 + dt.minute # минуты от полуночи

            return Bar(
                ticker=ticker,
                dt=dt,
                date_int=date_int,
                time_min=time_min,
                weekday=self._WEEKDAY_MAP[dt.weekday()],
                open=open_,
                high=high,
                low=low,
                close=close,
                vol=vol,
            )

        except (IndexError, ValueError) as e:
            logger.warning(f"Строка {line_num} пропущена ({e}): {row}")
            return None

    @staticmethod
    def _parse_dt(date_str: str, time_str: str) -> datetime:
        """Поддерживает HHMMSS и HHMM."""
        fmt = "%Y%m%d%H%M%S" if len(time_str) == 6 else "%Y%m%d%H%M"
        return datetime.strptime(date_str + time_str, fmt)
