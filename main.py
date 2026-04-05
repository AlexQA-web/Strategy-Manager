# main.py

import sys
import os
from loguru import logger
from config.settings import DATA_DIR, APP_NAME, APP_VERSION


def _load_fonts():
    """Загружает шрифты из папки fonts/ рядом с exe (для PyInstaller сборки)."""
    try:
        from PyQt6.QtGui import QFontDatabase
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        fonts_dir = os.path.join(base, 'fonts')
        if os.path.isdir(fonts_dir):
            for f in os.listdir(fonts_dir):
                if f.endswith('.ttf') or f.endswith('.otf'):
                    QFontDatabase.addApplicationFont(os.path.join(fonts_dir, f))
    except Exception as e:
        logger.warning(f"Не удалось загрузить шрифты: {e}")


def _setup_logging():
    """Настраивает loguru: ротируемый файл + stdout."""
    from config.settings import LOGS_DIR
    log_file = LOGS_DIR / "trading_manager_{time:YYYY-MM-DD}.log"
    logger.add(
        str(log_file),
        rotation="00:00",        # новый файл каждый день в полночь
        retention="14 days",     # хранить 14 дней
        compression="zip",       # архивировать старые
        level="DEBUG",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} — {message}",
        enqueue=True,            # thread-safe запись
    )


def main():
    _setup_logging()
    logger.info("═══════════════════════════════════════════")
    logger.info(f" {APP_NAME} v{APP_VERSION} — Запуск")
    logger.info("═══════════════════════════════════════════")

    DATA_DIR.mkdir(exist_ok=True)

    # Recovery: удаляем orphan .tmp файлы от предыдущего аварийного завершения
    from core.storage import cleanup_orphan_tmp
    cleanup_orphan_tmp()
    from core.chart_cache import cleanup_tmp_files
    cleanup_tmp_files()

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Иконка приложения
    icon_path = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'icon.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    _load_fonts()

    from ui.main_window import STYLE, MainWindow
    app.setStyleSheet(STYLE)

    window = MainWindow()
    window.show()

    from core.autostart import autoconnect_connectors, autostart_strategies, start_engine_watchdog
    autoconnect_connectors()
    autostart_strategies()
    # Watchdog: автоматически запускает/останавливает движки при изменении состояния коннекторов.
    # Проверяет каждые 15 секунд — при подключении коннектора стартует движки активных стратегий,
    # при отключении — останавливает. Работает вместе с расписанием.
    start_engine_watchdog(interval_sec=15)

    try:
        exit_code = app.exec()
    except Exception as e:
        logger.exception(f"Ошибка во время выполнения приложения: {e}")
        exit_code = 1
    finally:
        # Сброс equity на диск перед выходом
        try:
            from core.equity_tracker import flush_all
            flush_all()
        except Exception:
            pass
        # Останавливаем watchdog
        try:
            from core.autostart import stop_engine_watchdog
            stop_engine_watchdog()
        except Exception:
            pass
        # Останавливаем Telegram notifier
        try:
            from core.telegram_bot import get_notifier
            get_notifier().stop()
        except Exception:
            pass

    logger.info(f"{APP_NAME} — завершение работы")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
