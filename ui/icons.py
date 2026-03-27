from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon


_BASE_DIR = Path(__file__).resolve().parent.parent
_ICONS_DIR = _BASE_DIR / 'assets' / 'icons'


@lru_cache(maxsize=None)
def load_icon(relative_path: str) -> QIcon:
    path = _ICONS_DIR / relative_path
    if not path.exists():
        return QIcon()
    return QIcon(str(path))


def apply_icon(button, relative_path: str, size: int = 16):
    icon = load_icon(relative_path)
    button.setText('')
    button.setIcon(icon)
    button.setIconSize(QSize(size, size))
