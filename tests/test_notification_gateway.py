from unittest.mock import MagicMock, patch

from core.notification_gateway import NotificationGateway, WebhookNotifier


class TestNotificationGateway:

    def test_dispatch_routes_only_enabled_transports(self):
        gateway = NotificationGateway()
        sent = []

        with patch("core.notification_gateway.get_bool_setting", side_effect=lambda key: key != "webhook_enabled"), \
             patch("core.notification_gateway.get_setting", return_value=None):
            result = gateway.dispatch(
                "ORDER_FILLED",
                {
                    "telegram": lambda: sent.append("telegram") or True,
                    "ntfy": lambda: sent.append("ntfy") or True,
                    "webhook": lambda: sent.append("webhook") or True,
                },
                level_ok=lambda _: True,
            )

        assert result is True
        assert sent == ["telegram", "ntfy"]

    def test_dispatch_honors_per_transport_event_override(self):
        gateway = NotificationGateway()
        sent = []

        def _get_setting(key, default=None):
            if key == "notify_ntfy_ORDER_FILLED":
                return False
            return default

        with patch("core.notification_gateway.get_bool_setting", return_value=True), \
             patch("core.notification_gateway.get_setting", side_effect=_get_setting):
            gateway.dispatch(
                "ORDER_FILLED",
                {
                    "telegram": lambda: sent.append("telegram") or True,
                    "ntfy": lambda: sent.append("ntfy") or True,
                },
                level_ok=lambda _: True,
            )

        assert sent == ["telegram"]


class TestWebhookNotifier:

    def test_send_posts_json_payload(self):
        notifier = WebhookNotifier()
        notifier.configure("https://example.test/hook", enabled=True, headers={"X-Test": "1"})

        with patch("core.notification_gateway.requests.post") as mock_post:
            mock_post.return_value.status_code = 202
            mock_post.return_value.text = "ok"

            result = notifier.send(
                "hello",
                title="Trading Manager [ORDER_FILLED]",
                event_code="ORDER_FILLED",
                metadata={"agent": "sid-1"},
            )

        assert result is True
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["json"]["event_code"] == "ORDER_FILLED"
        assert mock_post.call_args.kwargs["headers"]["X-Test"] == "1"
