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

    from core.autostart import autoconnect_connectors, autostart_strategies
    autoconnect_connectors()
    autostart_strategies()

    exit_code = app.exec()

    # Сброс equity на диск перед выходом
    try:
        from core.equity_tracker import flush_all
        flush_all()
    except Exception:
        pass

    logger.info(f"{APP_NAME} — завершение работы")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
