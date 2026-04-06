from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import requests
from loguru import logger

from core.storage import get_bool_setting, get_setting


_TRANSPORT_ENABLED_KEYS = {
    "telegram": "telegram_enabled",
    "ntfy": "ntfy_enabled",
    "webhook": "webhook_enabled",
}


class WebhookNotifier:
    def __init__(self):
        self._url: Optional[str] = None
        self._enabled: bool = False
        self._timeout_sec: float = 10.0
        self._headers: dict[str, str] = {}

    def configure(
        self,
        url: str,
        *,
        enabled: bool = True,
        timeout_sec: float = 10.0,
        headers: Optional[dict[str, str]] = None,
    ):
        self._url = str(url or "").strip()
        self._enabled = bool(enabled and self._url)
        self._timeout_sec = max(float(timeout_sec or 10.0), 1.0)
        self._headers = dict(headers or {})
        if self._enabled:
            logger.info(f"Webhook notifier настроен: {self._url}")

    def load_from_settings(self):
        url = get_setting("webhook_url", "")
        enabled = get_bool_setting("webhook_enabled")
        timeout_sec = float(get_setting("webhook_timeout_sec", 10.0) or 10.0)
        headers = get_setting("webhook_headers", {})
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except Exception:
                headers = {}
        if not isinstance(headers, dict):
            headers = {}
        self.configure(url, enabled=enabled, timeout_sec=timeout_sec, headers=headers)

    def send(
        self,
        message: str,
        *,
        title: str = "Trading Manager",
        event_code: str = "",
        metadata: Optional[dict] = None,
    ) -> bool:
        if not self._enabled or not self._url:
            return False

        payload = {
            "title": title,
            "message": message,
            "event_code": str(event_code or ""),
            "metadata": dict(metadata or {}),
            "ts": time.time(),
        }
        headers = {"Content-Type": "application/json", **self._headers}

        try:
            response = requests.post(
                self._url,
                json=payload,
                headers=headers,
                timeout=self._timeout_sec,
            )
            if response.status_code in {200, 201, 202, 204}:
                return True
            logger.error(
                f"Webhook notifier error: {response.status_code} {response.text}"
            )
            return False
        except requests.RequestException as exc:
            logger.error(f"Webhook notifier network error: {exc}")
            return False
        except Exception as exc:
            logger.error(f"Webhook notifier unknown error: {exc}")
            return False


class NotificationGateway:
    def should_route(self, transport: str, event_code: str, level_ok: Callable[[str], bool]) -> bool:
        enabled_key = _TRANSPORT_ENABLED_KEYS.get(transport)
        if enabled_key and not get_bool_setting(enabled_key):
            return False

        setting_value = get_setting(f"notify_{transport}_{event_code}")
        if setting_value is not None:
            if isinstance(setting_value, bool):
                if not setting_value:
                    return False
            elif str(setting_value).lower() not in ("true", "1", "yes", "on"):
                return False

        return level_ok(event_code)

    def dispatch(
        self,
        event_code: str,
        senders: dict[str, Callable[[], bool]],
        *,
        level_ok: Callable[[str], bool],
    ) -> bool:
        sent_anywhere = False
        for transport, sender in senders.items():
            if not self.should_route(transport, event_code, level_ok):
                continue
            try:
                if sender():
                    sent_anywhere = True
            except Exception as exc:
                logger.error(f"Notification gateway send error [{transport}/{event_code}]: {exc}")
        return sent_anywhere

    def dispatch_raw(self, senders: dict[str, Callable[[], bool]]) -> bool:
        sent_anywhere = False
        for transport, sender in senders.items():
            enabled_key = _TRANSPORT_ENABLED_KEYS.get(transport)
            if enabled_key and not get_bool_setting(enabled_key):
                continue
            try:
                if sender():
                    sent_anywhere = True
            except Exception as exc:
                logger.error(f"Notification gateway raw send error [{transport}]: {exc}")
        return sent_anywhere


_webhook_notifier_instance: Optional[WebhookNotifier] = None
_webhook_notifier_lock = threading.Lock()
_notification_gateway = NotificationGateway()


def get_webhook_notifier() -> WebhookNotifier:
    global _webhook_notifier_instance
    if _webhook_notifier_instance is not None:
        return _webhook_notifier_instance
    with _webhook_notifier_lock:
        if _webhook_notifier_instance is None:
            _webhook_notifier_instance = WebhookNotifier()
        return _webhook_notifier_instance


webhook_notifier = get_webhook_notifier()
notification_gateway = _notification_gateway