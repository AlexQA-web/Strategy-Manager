from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QFormLayout, QGroupBox, QMessageBox, QSpinBox, QTimeEdit,
    QScrollArea, QFrame, QFileDialog,
)
from PyQt6.QtCore import Qt, QTime, QTimer
from loguru import logger

from core.storage import (
    get_exportable_settings,
    get_setting,
    get_bool_setting,
    set_setting as save_setting,
    get_all_schedules,
    SCHEDULES_FILE,
    _write,
)
from config.settings import APP_PROFILE_DIR
from core.telegram_bot import notifier
from core.scheduler import DAYS_RU
from ui.commission_settings_widget import CommissionSettingsWidget

STYLE = """
QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Arial;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 6px;
    background-color: #1e1e2e;
}
QTabBar::tab {
    background-color: #181825;
    color: #6c7086;
    padding: 8px 20px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: #89b4fa;
    border-bottom: 2px solid #89b4fa;
    background-color: #1e1e2e;
}
QTabBar::tab:hover { color: #cdd6f4; }
QGroupBox {
    border: 1px solid #313244;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px;
    color: #89b4fa;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}
QLineEdit, QComboBox, QSpinBox, QTimeEdit {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 5px 8px;
    color: #cdd6f4;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus {
    border: 1px solid #89b4fa;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 5px;
    padding: 6px 14px;
}
QPushButton:hover  { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QPushButton#btn_save {
    background-color: #ffffff;
    color: #1e1e2e;
    font-weight: bold;
    padding: 8px 24px;
}
QPushButton#btn_save:hover { background-color: #e0e0e0; }
QPushButton#btn_test {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_test:hover { background-color: #8ed490; }
QPushButton#btn_export {
    background-color: #ffffff;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_export:hover { background-color: #e0e0e0; }
QPushButton#btn_import {
    background-color: #ffffff;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_import:hover { background-color: #e0e0e0; }
QCheckBox { color: #cdd6f4; spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #585b70;
    border-radius: 3px;
    background-color: #181825;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QLabel#lbl_hint {
    color: #6c7086;
    font-size: 11px;
}
"""


class _SettingsMixin:
    """Миксин с логикой построения UI и сохранения настроек.

    Используется двумя классами:
      - SettingsWindow(QDialog, _SettingsMixin) — модальный диалог (для обратной совместимости)
      - SettingsWidget(QWidget, _SettingsMixin) — встраиваемый виджет для вкладки главного окна

    Роль: содержит все методы _tab_*, _build_schedule_group, _save_all.
    Вызывается из: MainWindow._build_settings_tab (SettingsWidget),
                   а также из кода, который ещё использует SettingsWindow напрямую.
    """

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._tab_finam(),              "🔌  Финам")
        self.tabs.addTab(self._tab_quik(),               "🖥  QUIK")
        self.tabs.addTab(self._tab_accounts(),           "🏦  Счета")
        self.tabs.addTab(self._tab_notifications(),      "🔔  Уведомления")
        self.tabs.addTab(self._tab_notification_events(), "📢  Типы уведомлений")
        self.tabs.addTab(self._tab_general(),            "⚙  Общие")
        self.tabs.addTab(self._tab_commissions(),        "💰  Комиссии")
        layout.addWidget(self.tabs, stretch=1)

        # Подключаем сигналы изменений всех виджетов для подсветки кнопки Сохранить
        self._connect_dirty_signals()

        _BTN_WHITE = (
            "QPushButton { background-color: #ffffff; color: #1e1e2e; font-weight: bold;"
            " border: none; border-radius: 5px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #e0e0e0; }"
            "QPushButton:pressed { background-color: #c8c8c8; }"
        )
        _BTN_GREEN = (
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e; font-weight: bold;"
            " border: none; border-radius: 5px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #8ed490; }"
            "QPushButton:pressed { background-color: #76c47a; }"
        )
        self._btn_save_style_white = _BTN_WHITE
        self._btn_save_style_green = _BTN_GREEN

        btn_bar = QWidget()
        btn_bar.setFixedHeight(56)
        btn_bar.setStyleSheet("background-color: #181825; border-top: 1px solid #313244;")
        bl = QHBoxLayout(btn_bar)
        bl.setContentsMargins(16, 10, 16, 10)

        # Кнопки экспорт/импорт — слева
        btn_export = QPushButton("📤  Сохранить в файл")
        btn_export.setFixedWidth(180)
        btn_export.setToolTip("Экспортировать все настройки в JSON-файл")
        btn_export.setStyleSheet(_BTN_WHITE)
        btn_export.clicked.connect(self._export_settings)
        bl.addWidget(btn_export)

        btn_import = QPushButton("📥  Загрузить из файла")
        btn_import.setFixedWidth(190)
        btn_import.setToolTip("Загрузить настройки из ранее сохранённого JSON-файла")
        btn_import.setStyleSheet(_BTN_WHITE)
        btn_import.clicked.connect(self._import_settings)
        bl.addWidget(btn_import)

        bl.addStretch()

        # Кнопка "Отмена" — только в диалоге, справа перед Сохранить
        if isinstance(self, QDialog):
            btn_cancel = QPushButton("Отмена")
            btn_cancel.setFixedWidth(90)
            btn_cancel.clicked.connect(self.reject)
            bl.addWidget(btn_cancel)

        self._btn_save = QPushButton("💾  Сохранить")
        self._btn_save.setFixedWidth(150)
        self._btn_save.setStyleSheet(_BTN_WHITE)
        self._btn_save.clicked.connect(self._save_all)
        bl.addWidget(self._btn_save)

        layout.addWidget(btn_bar)

    # ─────────────────────────────────────────────
    # Общий метод: блок расписания коннектора
    # Используется и для Финам, и для QUIK
    # ─────────────────────────────────────────────

    def _build_schedule_group(self, connector_id: str, title: str) -> QGroupBox:
        """
        Строит виджет расписания для коннектора connector_id.
        Сохраняет ссылки на виджеты как атрибуты self:
          self._<connector_id>_conn_time  — QTimeEdit время подключения
          self._<connector_id>_disc_time  — QTimeEdit время отключения
          self._<connector_id>_day_checks — dict[int, QCheckBox] дни недели
        """
        all_sched = get_all_schedules()
        sched     = all_sched.get(connector_id, {})

        group  = QGroupBox(title)
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        # Строка времени
        time_row = QHBoxLayout()

        time_row.addWidget(QLabel("Подключать с:"))
        conn_time = QTimeEdit()
        conn_time.setDisplayFormat("HH:mm")
        conn_time.setFixedWidth(80)
        t  = sched.get("connect_time", "06:50")
        h, m = map(int, t.split(":"))
        conn_time.setTime(QTime(h, m))
        time_row.addWidget(conn_time)

        time_row.addSpacing(20)
        time_row.addWidget(QLabel("Отключать в:"))
        disc_time = QTimeEdit()
        disc_time.setDisplayFormat("HH:mm")
        disc_time.setFixedWidth(80)
        t  = sched.get("disconnect_time", "23:45")
        h, m = map(int, t.split(":"))
        disc_time.setTime(QTime(h, m))
        time_row.addWidget(disc_time)

        time_row.addStretch()
        layout.addLayout(time_row)

        # Строка дней недели
        days_row   = QHBoxLayout()
        day_checks: dict[int, QCheckBox] = {}
        active_days = set(sched.get("days", [0, 1, 2, 3, 4]))
        days_row.addWidget(QLabel("Дни:"))
        for i, name in DAYS_RU.items():
            cb = QCheckBox(name)
            cb.setChecked(i in active_days)
            day_checks[i] = cb
            days_row.addWidget(cb)
        days_row.addStretch()
        layout.addLayout(days_row)

        # Сохраняем ссылки на виджеты в self — _save_all их считает
        setattr(self, f"_{connector_id}_conn_time",  conn_time)
        setattr(self, f"_{connector_id}_disc_time",  disc_time)
        setattr(self, f"_{connector_id}_day_checks", day_checks)

        return group

    # ─────────────────────────────────────────────
    # Вкладка: Финам
    # ─────────────────────────────────────────────

    def _tab_finam(self) -> QWidget:
        tab    = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        conn_group = QGroupBox("Параметры подключения TransAQ")
        form = QFormLayout(conn_group)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.finam_login = QLineEdit()
        self.finam_login.setText(get_setting("finam_login") or "")
        self.finam_login.setPlaceholderText("FZTC12345A")
        form.addRow("Номер коннектора:", self.finam_login)
        hint = QLabel("Номер из уведомления Финам")
        hint.setObjectName("lbl_hint")
        form.addRow("", hint)

        self.finam_password = QLineEdit()
        self.finam_password.setText(get_setting("finam_password") or "")
        self.finam_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.finam_password.setPlaceholderText("Пароль из SMS")
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self.finam_password)
        btn_show = QPushButton("👁")
        btn_show.setFixedWidth(34)
        btn_show.clicked.connect(lambda: self._toggle_echo(self.finam_password))
        pwd_row.addWidget(btn_show)
        form.addRow("Пароль:", pwd_row)

        self.finam_host = QLineEdit()
        self.finam_host.setText(get_setting("finam_host") or "tr1.finam.ru")
        form.addRow("Сервер:", self.finam_host)

        self.finam_port = QSpinBox()
        self.finam_port.setRange(1, 65535)
        self.finam_port.setValue(int(get_setting("finam_port") or 3900))
        self.finam_port.setFixedWidth(100)
        form.addRow("Порт:", self.finam_port)

        layout.addWidget(conn_group)

        # Расписание — общий метод
        layout.addWidget(self._build_schedule_group("finam", "Расписание Финам (МСК)"))

        # Тест подключения
        test_row = QHBoxLayout()
        self.lbl_finam_status = QLabel("")
        test_row.addWidget(self.lbl_finam_status)
        test_row.addStretch()
        btn_test = QPushButton("⚡ Тест подключения")
        btn_test.setObjectName("btn_test")
        btn_test.setFixedWidth(170)
        btn_test.clicked.connect(self._test_finam)
        test_row.addWidget(btn_test)
        layout.addLayout(test_row)

        layout.addStretch()
        return tab

    # ─────────────────────────────────────────────
    # Вкладка: QUIK
    # ─────────────────────────────────────────────

    def _tab_quik(self) -> QWidget:
        tab    = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        hint = QLabel(
            "Требует запущенного терминала QUIK с загруженным скриптом QLua_RPC.lua.\n"
            "Установка библиотеки:  pip install quik-lua-rpc"
        )
        hint.setObjectName("lbl_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        conn_group = QGroupBox("Параметры подключения QuikPy")
        form = QFormLayout(conn_group)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.quik_host = QLineEdit()
        self.quik_host.setText(get_setting("quik_host") or "localhost")
        form.addRow("Хост:", self.quik_host)

        self.quik_port = QSpinBox()
        self.quik_port.setRange(1, 65535)
        self.quik_port.setValue(int(get_setting("quik_port") or 34130))
        self.quik_port.setFixedWidth(100)
        form.addRow("Порт:", self.quik_port)

        layout.addWidget(conn_group)

        # Расписание — тот же общий метод, но для "quik"
        layout.addWidget(self._build_schedule_group("quik", "Расписание QUIK (МСК)"))

        # Тест подключения
        test_row = QHBoxLayout()
        self.lbl_quik_status = QLabel("")
        test_row.addWidget(self.lbl_quik_status)
        test_row.addStretch()
        btn_test = QPushButton("⚡ Тест подключения")
        btn_test.setObjectName("btn_test")
        btn_test.setFixedWidth(170)
        btn_test.clicked.connect(self._test_quik)
        test_row.addWidget(btn_test)
        layout.addLayout(test_row)

        layout.addStretch()
        return tab

    # ─────────────────────────────────────────────
    # Вкладка: Уведомления
    # ─────────────────────────────────────────────

    def _tab_notifications(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # Группа для Telegram
        tg_group = QGroupBox("Telegram")
        tg_form = QFormLayout(tg_group)
        tg_form.setSpacing(12)
        tg_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.tg_enabled = QCheckBox("Включить Telegram уведомления")
        self.tg_enabled.setChecked(get_bool_setting("telegram_enabled"))
        tg_form.addRow(self.tg_enabled)

        self.tg_token = QLineEdit()
        self.tg_token.setText(get_setting("telegram_token") or "")
        self.tg_token.setPlaceholderText("1234567890:AAF...")
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        token_row = QHBoxLayout()
        token_row.addWidget(self.tg_token)
        btn_show = QPushButton("👁")
        btn_show.setFixedWidth(34)
        btn_show.clicked.connect(lambda: self._toggle_echo(self.tg_token))
        token_row.addWidget(btn_show)
        tg_form.addRow("Bot Token:", token_row)
        hint = QLabel("Получи токен у @BotFather в Telegram")
        hint.setObjectName("lbl_hint")
        tg_form.addRow("", hint)

        self.tg_chat_id = QLineEdit()
        self.tg_chat_id.setText(str(get_setting("telegram_chat_id") or ""))
        self.tg_chat_id.setPlaceholderText("-100123456789")
        tg_form.addRow("Chat ID:", self.tg_chat_id)
        hint2 = QLabel("Узнай свой Chat ID у бота @userinfobot")
        hint2.setObjectName("lbl_hint")
        tg_form.addRow("", hint2)

        layout.addWidget(tg_group)

        # Группа для NTFY
        ntfy_group = QGroupBox("NTFY")
        ntfy_form = QFormLayout(ntfy_group)
        ntfy_form.setSpacing(12)
        ntfy_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.ntfy_enabled = QCheckBox("Включить NTFY уведомления")
        self.ntfy_enabled.setChecked(get_bool_setting("ntfy_enabled"))
        ntfy_form.addRow(self.ntfy_enabled)

        self.ntfy_server_url = QLineEdit()
        self.ntfy_server_url.setText(get_setting("ntfy_server_url") or "https://ntfy.sh")
        self.ntfy_server_url.setPlaceholderText("https://ntfy.sh")
        ntfy_form.addRow("Server URL:", self.ntfy_server_url)

        self.ntfy_topic = QLineEdit()
        self.ntfy_topic.setText(get_setting("ntfy_topic") or "")
        self.ntfy_topic.setPlaceholderText("my-topic-name")
        ntfy_form.addRow("Topic:", self.ntfy_topic)

        layout.addWidget(ntfy_group)

        # Кнопки тестирования
        test_row = QHBoxLayout()
        self.lbl_tg_status = QLabel("")
        test_row.addWidget(self.lbl_tg_status)
        self.lbl_ntfy_status = QLabel("")
        test_row.addWidget(self.lbl_ntfy_status)
        test_row.addStretch()

        btn_test_tg = QPushButton("✈ Тест Telegram")
        btn_test_tg.setObjectName("btn_test")
        btn_test_tg.setFixedWidth(120)
        btn_test_tg.clicked.connect(self._test_telegram)
        test_row.addWidget(btn_test_tg)

        btn_test_ntfy = QPushButton("🔔 Тест NTFY")
        btn_test_ntfy.setObjectName("btn_test")
        btn_test_ntfy.setFixedWidth(120)
        btn_test_ntfy.clicked.connect(self._test_ntfy)
        test_row.addWidget(btn_test_ntfy)

        layout.addLayout(test_row)

        layout.addStretch()
        return tab

    # ─────────────────────────────────────────────
    # Вкладка: Настройка уведомлений
    # ─────────────────────────────────────────────

    def _tab_notification_events(self) -> QWidget:
        from PyQt6.QtWidgets import QScrollArea
        from core.telegram_bot import EventCode

        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # Заголовок
        header_label = QLabel("Выберите, в каких каналах отправлять каждое уведомление:")
        header_label.setStyleSheet("font-weight: bold; color: #89b4fa; margin-bottom: 10px;")
        layout.addWidget(header_label)

        self._notify_telegram_checks: dict[str, QCheckBox] = {}
        self._notify_ntfy_checks: dict[str, QCheckBox] = {}

        groups = [
            ("Позиции", [
                (EventCode.POSITION_OPENED,  "Позиция открыта"),
                (EventCode.POSITION_CLOSED,  "Позиция закрыта"),
                (EventCode.MISSED_ENTRY,     "Сигнал есть, но позиция не открылась"),
                (EventCode.MISSED_EXIT,      "Сигнал есть, но позиция не закрылась"),
                (EventCode.STOP_LOSS_HIT,    "Сработал стоп-лосс"),
                (EventCode.TAKE_PROFIT_HIT,  "Сработал тейк-профит"),
            ]),
            ("Ордера", [
                (EventCode.ORDER_REJECTED,     "Ордер отклонён брокером"),
                (EventCode.ORDER_TIMEOUT,      "Ордер не исполнен вовремя"),
                (EventCode.ORDER_PARTIAL_FILL, "Частичное исполнение ордера"),
            ]),
            ("Коннектор", [
                (EventCode.CONNECTOR_CONNECTED,    "Подключение к брокеру"),
                (EventCode.CONNECTOR_DISCONNECTED, "Отвал коннектора"),
                (EventCode.CONNECTOR_ERROR,        "Ошибка соединения"),
                (EventCode.CONNECTOR_RECONNECTING, "Переподключение"),
            ]),
            ("Стратегии", [
                (EventCode.STRATEGY_STARTED,  "Стратегия запущена"),
                (EventCode.STRATEGY_STOPPED,  "Стратегия остановлена"),
                (EventCode.STRATEGY_ERROR,    "Ошибка в стратегии"),
                (EventCode.STRATEGY_CRASHED,  "Стратегия упала (критично)"),
            ]),
            ("Расписание и система", [
                (EventCode.SCHEDULE_CONNECT,    "Плановое подключение"),
                (EventCode.SCHEDULE_DISCONNECT, "Плановое отключение"),
                (EventCode.APP_STARTED,         "Приложение запущено"),
                (EventCode.APP_STOPPED,         "Приложение остановлено"),
            ]),
        ]

        for group_name, events in groups:
            grp = QGroupBox(group_name)
            grp_layout = QVBoxLayout(grp)
            grp_layout.setSpacing(6)
            
            for code, label in events:
                # Горизонтальный контейнер для метки и чекбоксов
                row_layout = QHBoxLayout()
                
                # Метка события
                event_label = QLabel(label)
                event_label.setMinimumWidth(300)  # Чтобы выравнивать чекбоксы
                row_layout.addWidget(event_label)
                
                # Чекбокс для Telegram
                telegram_key = f"notify_telegram_{code}"
                cb_telegram = QCheckBox("Telegram")
                # по умолчанию включены только важные события
                default = code in {
                    EventCode.MISSED_ENTRY, EventCode.MISSED_EXIT,
                    EventCode.POSITION_OPENED, EventCode.POSITION_CLOSED,
                    EventCode.ORDER_REJECTED, EventCode.ORDER_TIMEOUT,
                    EventCode.CONNECTOR_DISCONNECTED, EventCode.CONNECTOR_ERROR,
                    EventCode.STRATEGY_ERROR, EventCode.STRATEGY_CRASHED,
                }
                cb_telegram.setChecked(str(get_setting(telegram_key) or ("true" if default else "false")).lower() == "true")
                self._notify_telegram_checks[telegram_key] = cb_telegram
                row_layout.addWidget(cb_telegram)
                
                # Чекбокс для NTFY
                ntfy_key = f"notify_ntfy_{code}"
                cb_ntfy = QCheckBox("NTFY")
                cb_ntfy.setChecked(str(get_setting(ntfy_key) or ("true" if default else "false")).lower() == "true")
                self._notify_ntfy_checks[ntfy_key] = cb_ntfy
                row_layout.addWidget(cb_ntfy)
                
                row_layout.addStretch()  # Для выравнивания
                
                grp_layout.addLayout(row_layout)
                
            layout.addWidget(grp)

        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return tab

    # ─────────────────────────────────────────────
    # Вкладка: Счета
    # ─────────────────────────────────────────────

    def _tab_accounts(self) -> QWidget:
        """
        Вкладка переименования счетов брокера.

        Бизнес-логика:
          - При открытии пытается получить счета из подключённых коннекторов.
          - Если коннектор не подключён — показывает счета из known_accounts
            (settings.json, ключ "known_accounts": {"finam": [...], "quik": [...]}),
            которые сохраняются при каждом успешном получении счетов.
          - Кнопка «Обновить» перезапрашивает счета у коннекторов и перестраивает
            форму без закрытия окна настроек.

        Потребители: _SettingsMixin._save_all (читает self._alias_edits).
        """
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        self._alias_edits: dict[str, QLineEdit] = {}

        # Контейнер для групп счетов — будет перестраиваться при обновлении
        self._accounts_container = QWidget()
        self._accounts_layout = QVBoxLayout(self._accounts_container)
        self._accounts_layout.setContentsMargins(0, 0, 0, 0)
        self._accounts_layout.setSpacing(12)

        self._populate_accounts_groups()

        outer.addWidget(self._accounts_container)

        # Строка статуса + кнопка обновления
        refresh_row = QHBoxLayout()
        self._lbl_accounts_status = QLabel("")
        self._lbl_accounts_status.setObjectName("lbl_hint")
        refresh_row.addWidget(self._lbl_accounts_status)
        refresh_row.addStretch()
        btn_refresh = QPushButton("🔄  Обновить счета")
        btn_refresh.setFixedWidth(160)
        btn_refresh.clicked.connect(self._refresh_accounts)
        refresh_row.addWidget(btn_refresh)
        outer.addLayout(refresh_row)

        outer.addStretch()
        return tab

    def _populate_accounts_groups(self) -> bool:
        """
        Строит/перестраивает группы счетов внутри self._accounts_container.
        Вызывается при первом открытии и при нажатии «Обновить».

        Логика источника данных:
          1. Если коннектор подключён — берём счета из connector.get_accounts()
             и сохраняем их id в known_accounts для офлайн-режима.
          2. Если нет — берём id из known_accounts[connector_id] в settings.json.

        Возвращает True если хотя бы один счёт найден.
        """
        from core.connector_manager import connector_manager

        # Очищаем старые виджеты и словарь редакторов
        while self._accounts_layout.count():
            item = self._accounts_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._alias_edits.clear()

        aliases = get_setting("account_aliases") or {}
        known_accounts = get_setting("known_accounts") or {}
        any_accounts = False

        for connector_id, label in (("finam", "Финам (TransAQ)"), ("quik", "QUIK")):
            connector = connector_manager.get(connector_id)
            connected = connector and connector.is_connected()
            accounts = connector.get_accounts() if connected else []

            # Если подключены — обновляем кэш known_accounts
            if connected and accounts:
                known_accounts[connector_id] = [a.get("id", "") for a in accounts if a.get("id")]
                save_setting("known_accounts", known_accounts)

            # Fallback: если не подключён — берём из кэша known_accounts
            if not accounts:
                cached_ids = known_accounts.get(connector_id, [])
                accounts = [{"id": acc_id} for acc_id in cached_ids if acc_id]

            grp = QGroupBox(label)
            form = QFormLayout(grp)
            form.setSpacing(8)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

            if not accounts:
                lbl = QLabel("Нет подключения или счетов")
                lbl.setObjectName("lbl_hint")
                form.addRow(lbl)
            else:
                any_accounts = True
                for acc in accounts:
                    acc_id = acc.get("id", "")
                    if not acc_id or acc_id in self._alias_edits:
                        continue
                    edit = QLineEdit()
                    edit.setPlaceholderText(acc_id)
                    edit.setText(aliases.get(acc_id, ""))
                    self._alias_edits[acc_id] = edit
                    status = "🟢 " if connected else "💾 "
                    form.addRow(f"{status}{acc_id}:", edit)

            self._accounts_layout.addWidget(grp)

        return any_accounts

    def _refresh_accounts(self):
        """Перезапрашивает счета у коннекторов и перестраивает форму."""
        self._lbl_accounts_status.setText("⏳ Обновляем...")
        found = self._populate_accounts_groups()
        if found:
            self._lbl_accounts_status.setText("✅ Счета обновлены")
            self._lbl_accounts_status.setStyleSheet("color: #a6e3a1;")
        else:
            self._lbl_accounts_status.setText("⚠ Коннекторы не подключены, счета не найдены")
            self._lbl_accounts_status.setStyleSheet("color: #f9e2af;")

    # ─────────────────────────────────────────────
    # Вкладка: Общие
    # ─────────────────────────────────────────────

    def _tab_general(self) -> QWidget:
        tab    = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        app_group = QGroupBox("Поведение приложения")
        form = QFormLayout(app_group)
        form.setSpacing(12)

        self.chk_autoconnect = QCheckBox("Подключать коннекторы автоматически при запуске")
        self.chk_autoconnect.setChecked(
            str(get_setting("autoconnect") or "false").lower() == "true"
        )
        form.addRow(self.chk_autoconnect)

        self.chk_minimize_tray = QCheckBox("Сворачивать в системный трей при закрытии")
        self.chk_minimize_tray.setChecked(
            str(get_setting("minimize_to_tray") or "false").lower() == "true"
        )
        form.addRow(self.chk_minimize_tray)

        self.chk_start_strategies = QCheckBox("Запускать активные стратегии при старте")
        self.chk_start_strategies.setChecked(
            str(get_setting("autostart_strategies") or "false").lower() == "true"
        )
        form.addRow(self.chk_start_strategies)
        layout.addWidget(app_group)

        rc_group = QGroupBox("Переподключение (применяется к обоим коннекторам)")
        rc_form  = QFormLayout(rc_group)
        rc_form.setSpacing(10)

        self.spin_reconnect = QSpinBox()
        self.spin_reconnect.setRange(0, 20)
        self.spin_reconnect.setValue(int(get_setting("reconnect_attempts") or 5))
        self.spin_reconnect.setSuffix(" попыток")
        self.spin_reconnect.setFixedWidth(130)
        rc_form.addRow("Попыток:", self.spin_reconnect)

        self.spin_reconnect_delay = QSpinBox()
        self.spin_reconnect_delay.setRange(1, 120)
        self.spin_reconnect_delay.setValue(int(get_setting("reconnect_delay") or 5))
        self.spin_reconnect_delay.setSuffix(" сек")
        self.spin_reconnect_delay.setFixedWidth(130)
        rc_form.addRow("Пауза между попытками:", self.spin_reconnect_delay)

        layout.addWidget(rc_group)
        layout.addStretch()
        return tab

    def _tab_commissions(self) -> QWidget:
        """Вкладка настроек комиссий."""
        self._commission_widget = CommissionSettingsWidget()
        return self._commission_widget

    # ─────────────────────────────────────────────
    # Сохранение — всё в одном месте
    # ─────────────────────────────────────────────

    def _save_all(self):
        # Финам
        save_setting("finam_login",    self.finam_login.text().strip())
        save_setting("finam_password", self.finam_password.text())
        save_setting("finam_host",     self.finam_host.text().strip())
        save_setting("finam_port",     str(self.finam_port.value()))

        # QUIK
        save_setting("quik_host", self.quik_host.text().strip())
        save_setting("quik_port", str(self.quik_port.value()))

        # Псевдонимы счетов
        if hasattr(self, "_alias_edits"):
            aliases = {acc_id: edit.text().strip()
                       for acc_id, edit in self._alias_edits.items()
                       if edit.text().strip()}
            save_setting("account_aliases", aliases)

        # Расписания обоих коннекторов — читаем виджеты, пишем в schedules.json
        all_sched = get_all_schedules()
        for cid in ("finam", "quik"):
            conn_time  = getattr(self, f"_{cid}_conn_time",  None)
            disc_time  = getattr(self, f"_{cid}_disc_time",  None)
            day_checks = getattr(self, f"_{cid}_day_checks", {})
            if conn_time is None:
                continue
            all_sched[cid] = {
                "connect_time":    conn_time.time().toString("HH:mm"),
                "disconnect_time": disc_time.time().toString("HH:mm"),
                "days":            [i for i, cb in day_checks.items() if cb.isChecked()],
                "is_active":       True,
            }
        _write(SCHEDULES_FILE, all_sched)

        # Telegram
        save_setting("telegram_token",   self.tg_token.text().strip())
        save_setting("telegram_chat_id", self.tg_chat_id.text().strip())
        save_setting("telegram_enabled", "true" if self.tg_enabled.isChecked() else "false")
        # Обновляем состояние нотификатора сразу после сохранения
        notifier.load_from_settings()
        
        # NTFY
        save_setting("ntfy_server_url", self.ntfy_server_url.text().strip())
        save_setting("ntfy_topic", self.ntfy_topic.text().strip())
        save_setting("ntfy_enabled", "true" if self.ntfy_enabled.isChecked() else "false")
        
        # Уведомления для каждого канала
        for key, cb in self._notify_telegram_checks.items():
            save_setting(key, "true" if cb.isChecked() else "false")
        for key, cb in self._notify_ntfy_checks.items():
            save_setting(key, "true" if cb.isChecked() else "false")

        # Применяем настройки после сохранения (обновление ntfy/telegram)
        notifier.load_from_settings()

        # Общие
        save_setting("autoconnect",           "true" if self.chk_autoconnect.isChecked()       else "false")
        save_setting("minimize_to_tray",      "true" if self.chk_minimize_tray.isChecked()     else "false")
        save_setting("autostart_strategies",  "true" if self.chk_start_strategies.isChecked()  else "false")
        save_setting("reconnect_attempts",    str(self.spin_reconnect.value()))
        save_setting("reconnect_delay",       str(self.spin_reconnect_delay.value()))

        # Применяем без перезапуска
        notifier.load_from_settings()
        from core.connector_manager import connector_manager
        from core.scheduler import strategy_scheduler
        connector_manager.configure_all()
        strategy_scheduler.setup_connector_schedule()

        self._mark_clean()
        logger.info("Настройки сохранены")
        QMessageBox.information(self, "Настройки", "Настройки сохранены ✓")
        # accept() закрывает диалог — вызываем только если это QDialog
        if isinstance(self, QDialog):
            self.accept()

    # ─────────────────────────────────────────────
    # Тесты
    # ─────────────────────────────────────────────

    @staticmethod
    def _set_status_label(label: QLabel, text: str, color: str):
        """Потокобезопасно обновляет QLabel в главном потоке Qt."""
        QTimer.singleShot(0, lambda: (label.setText(text), label.setStyleSheet(f"color: {color};")))

    def _test_finam(self):
        import threading
        from core.finam_connector import finam_connector
        save_setting("finam_login",    self.finam_login.text().strip())
        save_setting("finam_password", self.finam_password.text())
        save_setting("finam_host",     self.finam_host.text().strip())
        save_setting("finam_port",     str(self.finam_port.value()))
        self._set_status_label(self.lbl_finam_status, "⏳ Подключаемся...", "#f9e2af")

        def _go():
            ok = finam_connector.connect()
            if ok:
                self._set_status_label(self.lbl_finam_status, "🟢 Подключено", "#a6e3a1")
            else:
                self._set_status_label(self.lbl_finam_status, "🔴 Ошибка подключения", "#f38ba8")

        threading.Thread(target=_go, daemon=True).start()

    def _test_quik(self):
        import threading
        from core.quik_connector import quik_connector
        save_setting("quik_host", self.quik_host.text().strip())
        save_setting("quik_port", str(self.quik_port.value()))
        self._set_status_label(self.lbl_quik_status, "⏳ Подключаемся...", "#f9e2af")

        def _go():
            ok = quik_connector.connect()
            if ok:
                self._set_status_label(self.lbl_quik_status, "🟢 Подключено", "#a6e3a1")
            else:
                self._set_status_label(self.lbl_quik_status, "🔴 Ошибка подключения", "#f38ba8")

        threading.Thread(target=_go, daemon=True).start()

    def _test_telegram(self):
        import threading
        save_setting("telegram_token",   self.tg_token.text().strip())
        save_setting("telegram_chat_id", self.tg_chat_id.text().strip())
        save_setting("telegram_enabled", "true" if self.tg_enabled.isChecked() else "false")
        notifier.load_from_settings()
        self._set_status_label(self.lbl_tg_status, "⏳ Отправляем...", "#f9e2af")

        def _go():
            ok, msg = notifier.test_connection_sync()
            if ok:
                self._set_status_label(self.lbl_tg_status, f"🟢 {msg}", "#a6e3a1")
            else:
                self._set_status_label(self.lbl_tg_status, f"🔴 {msg}", "#f38ba8")

        threading.Thread(target=_go, daemon=True).start()

    def _test_ntfy(self):
        import threading
        from core.ntfy_notifier import ntfy_notifier
        
        # Сохраняем настройки
        save_setting("ntfy_server_url", self.ntfy_server_url.text().strip())
        save_setting("ntfy_topic", self.ntfy_topic.text().strip())
        save_setting("ntfy_enabled", "true" if self.ntfy_enabled.isChecked() else "false")
        
        # Загружаем настройки в нотификатор
        ntfy_notifier.load_from_settings()
        
        self._set_status_label(self.lbl_ntfy_status, "⏳ Отправляем...", "#f9e2af")

        def _go():
            ok, msg = ntfy_notifier.test_connection()
            if ok:
                self._set_status_label(self.lbl_ntfy_status, f"🟢 {msg}", "#a6e3a1")
            else:
                self._set_status_label(self.lbl_ntfy_status, f"🔴 {msg}", "#f38ba8")

        threading.Thread(target=_go, daemon=True).start()

    # ─────────────────────────────────────────────
    # Отслеживание изменений (dirty-флаг)
    # ─────────────────────────────────────────────

    def _mark_dirty(self):
        """Вызывается при любом изменении виджета настроек.
        Окрашивает кнопку Сохранить в зелёный цвет.
        """
        if hasattr(self, "_btn_save"):
            self._btn_save.setStyleSheet(self._btn_save_style_green)

    def _mark_clean(self):
        """Сбрасывает dirty-флаг — кнопка Сохранить становится белой.
        Вызывается после успешного сохранения.
        """
        if hasattr(self, "_btn_save"):
            self._btn_save.setStyleSheet(self._btn_save_style_white)

    def _connect_dirty_signals(self):
        """Подключает сигналы изменений всех виджетов настроек к _mark_dirty.

        Охватывает:
          - QLineEdit: textChanged
          - QSpinBox: valueChanged
          - QCheckBox: stateChanged
          - QTimeEdit: timeChanged
        Вызывается из _build_ui после построения всех вкладок.
        """
        from PyQt6.QtWidgets import QLineEdit, QSpinBox, QCheckBox, QTimeEdit

        # Все QLineEdit, QSpinBox, QCheckBox, QTimeEdit внутри self
        for widget in self.findChildren(QLineEdit):
            widget.textChanged.connect(self._mark_dirty)
        for widget in self.findChildren(QSpinBox):
            widget.valueChanged.connect(self._mark_dirty)
        for widget in self.findChildren(QCheckBox):
            widget.stateChanged.connect(self._mark_dirty)
        for widget in self.findChildren(QTimeEdit):
            widget.timeChanged.connect(self._mark_dirty)

    # ─────────────────────────────────────────────
    # Обновление виджетов UI из файлов на диске
    # ─────────────────────────────────────────────

    def _reload_ui_from_disk(self):
        """Перечитывает все настройки с диска и обновляет виджеты.

        Вызывается после импорта настроек из файла.
        Обновляет все вкладки без пересоздания UI.
        """
        from core.storage import get_setting, get_all_schedules

        # --- Финам ---
        self.finam_login.setText(get_setting("finam_login") or "")
        self.finam_password.setText(get_setting("finam_password") or "")
        self.finam_host.setText(get_setting("finam_host") or "tr1.finam.ru")
        self.finam_port.setValue(int(get_setting("finam_port") or 3900))

        # --- QUIK ---
        self.quik_host.setText(get_setting("quik_host") or "localhost")
        self.quik_port.setValue(int(get_setting("quik_port") or 34130))

        # --- Расписания ---
        all_sched = get_all_schedules()
        for cid in ("finam", "quik"):
            sched = all_sched.get(cid, {})
            conn_time = getattr(self, f"_{cid}_conn_time", None)
            disc_time = getattr(self, f"_{cid}_disc_time", None)
            day_checks = getattr(self, f"_{cid}_day_checks", {})
            if conn_time is not None:
                t = sched.get("connect_time", "06:50")
                h, m = map(int, t.split(":"))
                conn_time.setTime(QTime(h, m))
            if disc_time is not None:
                t = sched.get("disconnect_time", "23:45")
                h, m = map(int, t.split(":"))
                disc_time.setTime(QTime(h, m))
            active_days = set(sched.get("days", [0, 1, 2, 3, 4]))
            for i, cb in day_checks.items():
                cb.setChecked(i in active_days)

        # --- Telegram ---
        self.tg_token.setText(get_setting("telegram_token") or "")
        self.tg_chat_id.setText(str(get_setting("telegram_chat_id") or ""))
        self.tg_enabled.setChecked(get_bool_setting("telegram_enabled"))

        # --- NTFY ---
        self.ntfy_server_url.setText(get_setting("ntfy_server_url") or "https://ntfy.sh")
        self.ntfy_topic.setText(get_setting("ntfy_topic") or "")
        self.ntfy_enabled.setChecked(get_bool_setting("ntfy_enabled"))

        # --- Уведомления для каждого канала ---
        for key, cb in self._notify_telegram_checks.items():
            default = "false"
            cb.setChecked(get_bool_setting(key))
        for key, cb in self._notify_ntfy_checks.items():
            default = "false"
            cb.setChecked(str(get_setting(key) or default).lower() == "true")

        # --- Счета ---
        if hasattr(self, "_populate_accounts_groups"):
            self._populate_accounts_groups()

        # --- Общие ---
        self.chk_autoconnect.setChecked(
            str(get_setting("autoconnect") or "false").lower() == "true"
        )
        self.chk_minimize_tray.setChecked(
            str(get_setting("minimize_to_tray") or "false").lower() == "true"
        )
        self.chk_start_strategies.setChecked(
            str(get_setting("autostart_strategies") or "false").lower() == "true"
        )
        self.spin_reconnect.setValue(int(get_setting("reconnect_attempts") or 5))
        self.spin_reconnect_delay.setValue(int(get_setting("reconnect_delay") or 5))

        # --- Комиссии ---
        if hasattr(self, "_commission_widget"):
            self._commission_widget._load_settings()

        # Сбрасываем dirty-флаг (данные теперь совпадают с диском)
        self._mark_clean()

    def _import_settings(self):
        """Загружает настройки из выбранного пользователем JSON-файла и применяет их.

        Бизнес-логика:
          - Открывает QFileDialog с начальной папкой app_profile/.
          - Ожидает формат {"settings": {...}, "schedules": {...}, "commissions": {...}}.
          - Записывает settings через save_settings, schedules через _write(SCHEDULES_FILE).
          - Если присутствует ключ commissions — обновляет commission_config.json,
            перегружает commission_manager и instrument_classifier.
          - Обновляет все виджеты UI динамически (без перезагрузки окна).

        Вызывается: кнопкой "Загрузить из файла" в панели кнопок _build_ui.
        """
        import json as _json
        from core.storage import save_settings, get_setting, SCHEDULES_FILE, _write

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Загрузить настройки из файла",
            str(APP_PROFILE_DIR),
            "JSON файлы (*.json);;Все файлы (*)",
        )
        if not path:
            return  # пользователь отменил

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            logger.error(f"Ошибка чтения файла настроек: {e}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось прочитать файл:\n{e}")
            return

        # Валидация структуры
        if not isinstance(data, dict) or "settings" not in data:
            QMessageBox.critical(
                self, "Ошибка формата",
                "Файл не является корректным файлом настроек Trading Manager.\n"
                "Ожидается формат: {\"settings\": {...}, \"schedules\": {...}}"
            )
            return

        has_strategies = bool(data.get("strategies"))
        strategies_note = (
            f"\nТакже будет импортировано стратегий: {len(data['strategies'])}"
            if has_strategies else ""
        )
        reply = QMessageBox.question(
            self, "Загрузить настройки",
            f"Загрузить настройки из файла?\n{path}\n\n"
            f"Текущие настройки будут перезаписаны.{strategies_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            save_settings(data["settings"])
            if "schedules" in data and isinstance(data["schedules"], dict):
                _write(SCHEDULES_FILE, data["schedules"])

            # Восстанавливаем настройки комиссий
            if "commissions" in data and isinstance(data["commissions"], dict):
                from core.commission_manager import commission_manager
                from core.instrument_classifier import instrument_classifier

                comm_data = data["commissions"]
                commission_manager.config = comm_data
                commission_manager.save_config()

                if "prefix_rules" in comm_data:
                    instrument_classifier.prefix_rules = comm_data["prefix_rules"]
                if "manual_mapping" in comm_data:
                    instrument_classifier.manual_mapping = comm_data["manual_mapping"]
                instrument_classifier.save_config()

            # Восстанавливаем стратегии
            if "strategies" in data and isinstance(data["strategies"], dict):
                from core.storage import STRATEGIES_FILE, _write
                strategies_data = data["strategies"]
                if strategies_data:
                    _write(STRATEGIES_FILE, strategies_data)
                    logger.info(f"Импортировано стратегий: {len(strategies_data)}")

            logger.info(f"Настройки импортированы из {path}")
        except OSError as e:
            logger.error(f"Ошибка записи настроек при импорте: {e}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось применить настройки:\n{e}")
            return

        # Применяем без перезапуска
        notifier.load_from_settings()
        from core.connector_manager import connector_manager
        from core.scheduler import strategy_scheduler
        connector_manager.configure_all()
        strategy_scheduler.setup_connector_schedule()

        # Обновляем все виджеты UI
        self._reload_ui_from_disk()

        # Обновляем таблицу агентов если стратегии были импортированы
        if data.get("strategies"):
            try:
                from ui.main_window import ui_signals
                ui_signals.strategies_changed.emit()
            except Exception:
                pass

        QMessageBox.information(
            self, "Настройки загружены",
            "Настройки успешно загружены из файла и применены."
        )

    def _export_settings(self):
        """Сохраняет все настройки приложения (включая чувствительные данные) в выбранный пользователем JSON-файл.

        Бизнес-логика:
          - Собирает settings.json (включая секреты) + schedules.json + commission_config.json + strategies.json.
          - Открывает QFileDialog для выбора пути сохранения.
          - Записывает файл с отступами (indent=2) в UTF-8.
          - Экспортирует токены, пароли, логины и идентификаторы счетов.

        Вызывается: кнопкой "Сохранить в файл" в панели кнопок _build_ui.
        """
        import json as _json
        from core.storage import get_all_schedules
        from core.commission_manager import commission_manager
        from core.instrument_classifier import instrument_classifier

        from core.storage import get_all_strategies, get_settings
        commission_data = dict(commission_manager.config)
        commission_data['prefix_rules'] = dict(instrument_classifier.prefix_rules)
        commission_data['manual_mapping'] = dict(instrument_classifier.manual_mapping)

        export_data = {
            'settings':   get_settings(),
            'schedules':  get_all_schedules(),
            'commissions': commission_data,
            'strategies': get_all_strategies(),
        }

        default_path = str(APP_PROFILE_DIR / 'trading_manager_settings.full.json')
        path, _ = QFileDialog.getSaveFileName(
            self,
            'Сохранить настройки в файл',
            default_path,
            'JSON файлы (*.json);;Все файлы (*)',
        )
        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                _json.dump(export_data, f, ensure_ascii=False, indent=2)
            logger.info(f'Все настройки (включая чувствительные) экспортированы в {path}')
            QMessageBox.information(
                self,
                'Экспорт настроек',
                f'Все настройки (включая чувствительные данные) сохранены в файл:\n{path}\n\n'
                'ВНИМАНИЕ: Файл содержит логины, пароли и другие чувствительные данные!',
            )
        except OSError as e:
            logger.error(f'Ошибка экспорта настроек: {e}')
            QMessageBox.critical(self, 'Ошибка', f'Не удалось сохранить файл:\n{e}')

    # ─────────────────────────────────────────────
    # Утилиты
    # ─────────────────────────────────────────────

    @staticmethod
    def _toggle_echo(field: QLineEdit):
        if field.echoMode() == QLineEdit.EchoMode.Password:
            field.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)


class SettingsWindow(QDialog, _SettingsMixin):
    """Модальный диалог настроек (обратная совместимость).

    Вызывается из кода, который использует exec() напрямую.
    Содержит кнопку "Отмена" и закрывается через accept() после сохранения.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumSize(600, 560)
        self.resize(660, 620)
        self.setStyleSheet(STYLE)
        self._build_ui()


class SettingsWidget(QWidget, _SettingsMixin):
    """Встраиваемый виджет настроек для вкладки QTabWidget главного окна.

    Идентичен SettingsWindow по содержимому, но без кнопки "Отмена" и без
    модального поведения QDialog. Используется в MainWindow._build_settings_tab.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(STYLE)
        self._build_ui()
