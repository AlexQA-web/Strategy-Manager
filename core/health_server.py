"""
Health Check HTTP Server — мониторинг состояния системы извне.

Предоставляет REST endpoints для проверки состояния:
- GET /health — общий статус коннекторов, стратегий, позиций
- GET /metrics — PnL, drawdown, количество сделок
- GET /health/strategies — статус каждой стратегии

Запускается в отдельном потоке при старте приложения.
"""

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional
from loguru import logger


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP handler для health check endpoints."""

    # Глобальные колбэки для получения данных
    health_callback: Optional[Callable[[], Dict[str, Any]]] = None
    metrics_callback: Optional[Callable[[], Dict[str, Any]]] = None
    strategies_callback: Optional[Callable[[], Dict[str, Any]]] = None
    auth_token: Optional[str] = None

    def do_GET(self):
        """Обработка GET запросов."""
        if self.auth_token and not self._check_auth():
            self._send_json(401, {'error': 'Unauthorized'})
            return
        if self.path == '/health':
            self._handle_health()
        elif self.path == '/metrics':
            self._handle_metrics()
        elif self.path == '/health/strategies':
            self._handle_strategies()
        elif self.path == '/ready':
            self._handle_ready()
        else:
            self._send_json(404, {'error': 'Not found'})

    def _check_auth(self) -> bool:
        """Проверяет Bearer-токен в заголовке Authorization."""
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:] == self.auth_token
        return False

    def _handle_health(self):
        """GET /health — общий статус системы."""
        if self.health_callback:
            try:
                data = self.health_callback()
                self._send_json(200, data)
            except Exception as e:
                logger.error(f'[HealthServer] Ошибка в health_callback: {e}')
                self._send_json(500, {'error': str(e), 'status': 'error'})
        else:
            self._send_json(200, {'status': 'ok', 'message': 'Health check не настроен'})

    def _handle_metrics(self):
        """GET /metrics — метрики системы."""
        if self.metrics_callback:
            try:
                data = self.metrics_callback()
                self._send_json(200, data)
            except Exception as e:
                logger.error(f'[HealthServer] Ошибка в metrics_callback: {e}')
                self._send_json(500, {'error': str(e)})
        else:
            self._send_json(200, {'message': 'Metrics не настроены'})

    def _handle_strategies(self):
        """GET /health/strategies — статус стратегий."""
        if self.strategies_callback:
            try:
                data = self.strategies_callback()
                self._send_json(200, data)
            except Exception as e:
                logger.error(f'[HealthServer] Ошибка в strategies_callback: {e}')
                self._send_json(500, {'error': str(e)})
        else:
            self._send_json(200, {'message': 'Strategies status не настроен'})

    def _handle_ready(self):
        """GET /ready — проверка готовности приложения."""
        self._send_json(200, {'ready': True, 'timestamp': time.time()})

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        """Отправить JSON ответ."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        """Подавить стандартное логирование HTTP сервера."""
        logger.debug(f'[HealthServer] {format % args}')


class HealthServer:
    """
    HTTP сервер для health check endpoints.
    
    Запускается в отдельном потоке и не блокирует основной поток приложения.
    """

    _ALLOWED_HOSTS = ('127.0.0.1', 'localhost', '::1')

    def __init__(self, host: str = '127.0.0.1', port: int = 8080,
                 enabled: bool = False, auth_token: Optional[str] = None):
        if host not in self._ALLOWED_HOSTS:
            logger.warning(
                f'[HealthServer] Привязка к {host} запрещена, принудительно 127.0.0.1'
            )
            host = '127.0.0.1'
        self._host: str = host
        self._port: int = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._enabled: bool = enabled
        self._auth_token: Optional[str] = auth_token

        logger.info(f'[HealthServer] Инициализирован: {host}:{port} (enabled={enabled})')

    def set_health_callback(self, callback: Callable[[], Dict[str, Any]]):
        """Установить колбэк для /health endpoint."""
        HealthCheckHandler.health_callback = callback

    def set_metrics_callback(self, callback: Callable[[], Dict[str, Any]]):
        """Установить колбэк для /metrics endpoint."""
        HealthCheckHandler.metrics_callback = callback

    def set_strategies_callback(self, callback: Callable[[], Dict[str, Any]]):
        """Установить колбэк для /health/strategies endpoint."""
        HealthCheckHandler.strategies_callback = callback

    def start(self):
        """Запустить HTTP сервер в отдельном потоке."""
        if not self._enabled:
            logger.debug('[HealthServer] Отключён в настройках, start() пропущен')
            return
        if self._running:
            logger.warning('[HealthServer] Уже запущен')
            return

        try:
            HealthCheckHandler.auth_token = self._auth_token
            self._server = HTTPServer((self._host, self._port), HealthCheckHandler)
            self._running = True
            self._thread = threading.Thread(
                target=self._serve, daemon=True, name='health-server'
            )
            self._thread.start()
            logger.info(f'[HealthServer] Запущен на http://{self._host}:{self._port}')
        except OSError as e:
            logger.error(f'[HealthServer] Ошибка запуска: {e}')
            self._running = False

    def stop(self):
        """Остановить HTTP сервер."""
        if not self._running:
            return

        self._running = False
        if self._server:
            self._server.shutdown()
            self._server = None
        logger.info('[HealthServer] Остановлен')

    def _serve(self):
        """Цикл обработки HTTP сервера."""
        try:
            self._server.serve_forever()
        except Exception as e:
            logger.error(f'[HealthServer] Ошибка serve_forever: {e}')
        finally:
            self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def url(self) -> str:
        return f'http://{self._host}:{self._port}'


# Синглтон для использования в приложении
from core.storage import get_setting, get_bool_setting
_port = int(get_setting('health_server_port') or 8080)
_enabled = get_bool_setting('health_server_enabled')  # по умолчанию False
_auth_token = get_setting('health_server_token') or None
health_server: HealthServer = HealthServer(
    port=_port, enabled=_enabled, auth_token=_auth_token,
)
