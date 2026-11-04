#Remote To Local Copy(RTLC) — веб-интерфейс (настройки, статус, управление)
import asyncio
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time

import uvicorn
from fastapi import (Depends, FastAPI, HTTPException, Request, Security,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import rtlc
from config import Config
from rtlc import CopyUtility

MASK = '***'
STATE_FILE = 'rtlc.state.json'
LOG_FILE = os.path.join('Logs', 'rtlc.log')

# ini-имя -> (атрибут Config, тип); 'secret' маскируется в GET и
# не перезаписывается значением-маской в PUT
EDITABLE = {
    'start_date': ('startDate', str),
    'last_file': ('timeStamp', float),
    'refresh_time': ('refreshTime', int),
    'extension_file': ('extensionFile', str),
    'stabilize_time': ('stabilizeTime', float),
    'remote': ('remotePath', str),
    'local': ('localPath', str),
    'use_network_share': ('useNetShare', bool),
    'share_ip': ('shareIP', str),
    'share_port': ('sharePort', int),
    'share_name': ('shareName', str),
    'user_name': ('userName', str),
    'user_password': ('userPassword', 'secret'),
    'client_machine_name': ('clientMachineName', str),
    'remote_machine_name': ('remoteMachineName', str),
    'logs_level': ('logsLevel', str),
    'logs_size': ('logsSize', int),
    'logs_backups': ('logsBackups', int),
    'web_host': ('webHost', str),
    'web_port': ('webPort', int),
    'web_trust_loopback': ('webTrustLoopback', bool),
    'web_api_key': ('webApiKey', 'secret'),
    'to_folder': ('toFolder', bool),
    'to_dicom': ('toDicom', bool),
    'dicom_host': ('dicomHost', str),
    'dicom_port': ('dicomPort', int),
    'dicom_called_aet': ('dicomCalledAet', str),
    'dicom_calling_aet': ('dicomCallingAet', str),
    'dicom_timeout': ('dicomTimeout', int),
    'dicom_retry_initial': ('dicomRetryInitial', int),
    'dicom_retry_max': ('dicomRetryMax', int),
    'dicom_breaker_threshold': ('dicomBreakerThreshold', int),
    'dicom_breaker_cooldown': ('dicomBreakerCooldown', int),
    'dicom_queue_tick': ('dicomQueueTick', int),
}

LOG_LEVELS = {'NOTSET', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}


class Engine:
    """Владеет экземпляром CopyUtility и потоком его цикла."""

    def __init__(self):
        self.utility = None
        self.thread = None
        self.last_start_error = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return (self.utility is not None and self.thread is not None
                and self.thread.is_alive())

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            try:
                self.utility = CopyUtility()
            except SystemExit:
                self.utility = None
                self.last_start_error = (
                    'Запуск прерван: проверьте конфигурацию '
                    '(подробности — Logs/rtlc.log).')
                return False
            except Exception as exc:
                self.utility = None
                self.last_start_error = f'{type(exc).__name__}: {exc}'
                return False
            self.last_start_error = None
            self.utility.stop_event.clear()
            self.thread = threading.Thread(target=self.utility.run_forever,
                                           daemon=True,
                                           name='rtlc-engine')
            self.thread.start()
            return True

    def stop(self):
        with self._lock:
            if self.utility is not None:
                self.utility.stop_event.set()
            thread = self.thread
        if thread is not None:
            thread.join(timeout=15)

    def restart(self) -> bool:
        self.stop()
        return self.start()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def _trusted_loopback(request) -> bool:
    """Локальный запрос без ключа: клиент и Host — loopback (Host проверяется
    против DNS rebinding, иначе чужая страница выдала бы себя за свою)."""
    cfg = request.app.state.web_config
    if not cfg.webTrustLoopback:
        return False
    client = request.client.host if request.client else ''
    if not _is_loopback(client):
        return False
    host = request.headers.get('host', '')
    hostname = host.rsplit(':', 1)[0].strip('[]')
    return _is_loopback(hostname)


api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)


async def verify_api_key(request: Request,
                         api_key: str | None = Security(api_key_header)):
    if _trusted_loopback(request):
        return api_key or ''
    cfg = request.app.state.web_config
    if not cfg.webApiKey:
        raise HTTPException(status_code=503, detail='Web API key not configured')
    if not api_key:
        raise HTTPException(status_code=401, detail='Missing X-API-Key header')
    if not hmac.compare_digest(api_key.encode('utf-8'),
                               cfg.webApiKey.encode('utf-8')):
        raise HTTPException(status_code=403, detail='Invalid API key')
    return api_key


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ограничение для сетевых клиентов (перебор ключа), когда Web/host
    открыт наружу. Loopback и статика не ограничиваются."""
    EXEMPT_PATHS = ('/health', '/')

    def __init__(self, app, max_requests: int = 600, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests = {}

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else 'unknown'
        if _is_loopback(client_ip) or request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)
        now = time.time()
        reqs = [t for t in self._requests.get(client_ip, [])
                if now - t < self.window_seconds]
        if len(reqs) >= self.max_requests:
            return JSONResponse(status_code=429,
                                content={'detail': 'Rate limit exceeded'})
        reqs.append(now)
        self._requests[client_ip] = reqs
        return await call_next(request)


def status_snapshot(app: FastAPI) -> dict:
    engine = app.state.engine
    snap = {
        'time': time.time(),
        'engine': 'stopped',
        'last_start_error': engine.last_start_error,
        'utility': None
    }
    if engine.utility is not None:
        snap['engine'] = 'running' if engine.running else 'stopped'
        snap['utility'] = engine.utility.status()
    return snap


def _coerce(value, kind, key):
    try:
        if kind is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ('true', '1', 'yes', 'on'):
                    return True
                if v in ('false', '0', 'no', 'off'):
                    return False
            raise ValueError
        if kind is int:
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if kind is float:
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f'Invalid value for "{key}": {value!r}')


def _validate(key, value):
    if key == 'refresh_time' and value < 1:
        raise HTTPException(400, 'refresh_time must be >= 1')
    if key == 'stabilize_time' and value < 0:
        raise HTTPException(400, 'stabilize_time must be >= 0')
    if key == 'web_port' and not 1 <= value <= 65535:
        raise HTTPException(400, 'web_port must be in 1..65535')
    if key == 'dicom_port' and not 1 <= value <= 65535:
        raise HTTPException(400, 'dicom_port must be in 1..65535')
    if key == 'share_port' and not 1 <= value <= 65535:
        raise HTTPException(400, 'share_port must be in 1..65535')
    if key == 'dicom_timeout' and value < 1:
        raise HTTPException(400, 'dicom_timeout must be >= 1')
    if key == 'dicom_retry_initial' and value < 1:
        raise HTTPException(400, 'dicom_retry_initial must be >= 1')
    if key == 'dicom_retry_max' and value < 1:
        raise HTTPException(400, 'dicom_retry_max must be >= 1')
    if key == 'dicom_breaker_threshold' and value < 1:
        raise HTTPException(400, 'dicom_breaker_threshold must be >= 1')
    if key == 'dicom_breaker_cooldown' and value < 1:
        raise HTTPException(400, 'dicom_breaker_cooldown must be >= 1')
    if key == 'dicom_queue_tick' and value < 1:
        raise HTTPException(400, 'dicom_queue_tick must be >= 1')
    if key in ('dicom_called_aet', 'dicom_calling_aet') and (
            len(value) > 16 or not value.strip() == value):
        raise HTTPException(400, f'{key}: AE Title — до 16 символов без пробелов по краям')
    if key == 'logs_level' and value.upper() not in LOG_LEVELS:
        raise HTTPException(400, f'logs_level must be one of {sorted(LOG_LEVELS)}')
    if key == 'remote' and not value.strip():
        raise HTTPException(400, 'remote path must not be empty')
    if key == 'web_api_key' and len(value) < 8:
        raise HTTPException(400, 'web_api_key must be at least 8 characters')


def create_app(engine: Engine | None = None) -> FastAPI:
    os.chdir(rtlc.getModulePath())
    engine = engine or Engine()

    app = FastAPI(title='RTLC', docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.add_middleware(RateLimitMiddleware)

    web_cfg = Config('config.ini')
    web_cfg.load()
    if not web_cfg.webApiKey:
        web_cfg.webApiKey = secrets.token_urlsafe(24)
        web_cfg.save()
    app.state.web_config = web_cfg

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/', include_in_schema=False)
    async def index():
        return FileResponse(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'static', 'index.html'))

    @app.get('/api/v1/status')
    async def get_status(_=Depends(verify_api_key)):
        return status_snapshot(app)

    @app.post('/api/v1/control/{action}')
    async def control(action: str, _=Depends(verify_api_key)):
        eng = app.state.engine
        if action == 'start':
            ok = await run_in_threadpool(eng.start)
        elif action == 'stop':
            await run_in_threadpool(eng.stop)
            ok = True
        elif action == 'restart':
            ok = await run_in_threadpool(eng.restart)
        elif action == 'scan_now':
            if not eng.running:
                raise HTTPException(409, 'Engine is not running')
            await run_in_threadpool(eng.utility.walk_cycle)
            ok = True
        else:
            raise HTTPException(404, f'Unknown action: {action}')
        if not ok:
            raise HTTPException(500, eng.last_start_error or 'start failed')
        return status_snapshot(app)

    @app.post('/api/v1/dicom/echo')
    async def dicom_echo(_=Depends(verify_api_key)):
        cfg = Config('config.ini')
        cfg.load()
        if not cfg.toDicom:
            raise HTTPException(409, 'DICOM delivery is disabled (to_dicom=false)')
        try:
            import dicom_sender
        except ImportError as exc:
            raise HTTPException(503, f'pynetdicom/pydicom not available: {exc}')

        def _ping():
            return dicom_sender.echo(cfg.dicomHost, cfg.dicomPort,
                                     cfg.dicomCalledAet, cfg.dicomCallingAet,
                                     cfg.dicomTimeout)

        ok, msg = await run_in_threadpool(_ping)
        return {'ok': ok, 'message': msg,
                'node': f'{cfg.dicomCalledAet}@{cfg.dicomHost}:{cfg.dicomPort}'}

    def _queue_or_404(app):
        eng = app.state.engine
        if eng.utility is None or eng.utility.dicom_queue is None:
            raise HTTPException(409,
                                'Engine is not running or DICOM delivery '
                                'is disabled')
        return eng.utility.dicom_queue

    @app.get('/api/v1/dicom/queue')
    async def dicom_queue_status(_=Depends(verify_api_key)):
        eng = app.state.engine
        if eng.utility is None or eng.utility.dicom_queue is None:
            return {'summary': None, 'items': []}
        q = eng.utility.dicom_queue
        return {'summary': q.summary(), 'items': q.items()}

    @app.post('/api/v1/dicom/queue/retry_now')
    async def dicom_queue_retry(_=Depends(verify_api_key)):
        q = _queue_or_404(app)
        q.retry_now()
        return {'ok': True, 'summary': q.summary()}

    @app.post('/api/v1/dicom/queue/purge_permanent')
    async def dicom_queue_purge(_=Depends(verify_api_key)):
        q = _queue_or_404(app)
        import csv
        from time import strftime
        purged = []
        with q.u.state_lock:
            for path, entry in list(q.u.state['files'].items()):
                d = entry.get('dicom')
                if d and d.get('permanent'):
                    purged.append((path, entry.get('size'),
                                   entry.get('mtime'), d.get('attempts'),
                                   d.get('last_error')))
                    del q.u.state['files'][path]
            if purged:
                q.u.saveState()
        if purged:
            os.makedirs('Logs', exist_ok=True)
            name = f'Logs/dicom_permanent_{strftime("%Y%m%d_%H%M%S")}.csv'
            with open(name, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerows(
                    [['path', 'size', 'mtime', 'attempts', 'last_error']] +
                    purged)
        return {'purged': len(purged),
                'archive': name if purged else None}

    @app.get('/api/v1/config')
    async def get_config(_=Depends(verify_api_key)):
        cfg = Config('config.ini')
        cfg.load()
        out = {}
        for key, (attr, kind) in EDITABLE.items():
            value = getattr(cfg, attr)
            out[key] = MASK if kind == 'secret' and value else value
        return out

    @app.put('/api/v1/config')
    async def put_config(request: Request, _=Depends(verify_api_key)):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, 'JSON body expected')
        if not isinstance(body, dict):
            raise HTTPException(400, 'JSON object expected')

        cfg = Config('config.ini')
        cfg.load()
        unknown = sorted(set(body) - set(EDITABLE))
        if unknown:
            raise HTTPException(400, f'Unknown options: {unknown}')

        for key, raw in body.items():
            attr, kind = EDITABLE[key]
            if kind == 'secret' and raw == MASK:
                continue
            value = _coerce(raw, kind if kind != 'secret' else str, key)
            _validate(key, value)
            setattr(cfg, attr, value)

        remote = getattr(cfg, 'remotePath')
        local = getattr(cfg, 'localPath')
        if not cfg.toFolder and not cfg.toDicom:
            raise HTTPException(
                400, 'Both delivery sinks are disabled '
                '(to_folder=false, to_dicom=false) — nothing to do')
        if cfg.toDicom:
            if not cfg.dicomHost.strip() or not cfg.dicomCalledAet.strip():
                raise HTTPException(
                    400, 'to_dicom=true требует заполненные '
                    'dicom_host и dicom_called_aet')
        if cfg.toFolder:
            if not local.strip():
                raise HTTPException(
                    400, 'local path must not be empty when to_folder=true')
            if not cfg.useNetShare and CopyUtility.pathContains(
                    remote, local):
                raise HTTPException(
                    400, 'Local folder is the remote folder or inside it '
                    '(copy loop) — configuration rejected')

        result = cfg.save()
        app.state.web_config = cfg
        eng = app.state.engine
        was_running = eng.running or eng.utility is not None
        if was_running:
            await run_in_threadpool(eng.restart)
        return {'saved': result, 'engine': status_snapshot(app)['engine'],
                'last_start_error': eng.last_start_error}

    @app.get('/api/v1/ledger')
    async def get_ledger(limit: int = 200, offset: int = 0,
                         _=Depends(verify_api_key)):
        eng = app.state.engine
        files = {}
        if eng.utility is not None:
            files = eng.utility.state.get('files', {})
        elif os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    files = json.load(f).get('files', {})
            except Exception:
                files = {}
        items = []
        for path, meta in files.items():
            folder = meta.get('folder') or {}
            items.append({
                'path': path,
                # dest/copied_at на верхнем уровне — формат журнала до 2.0
                'dest': folder.get('dest') or meta.get('dest'),
                'size': meta.get('size'),
                'mtime': meta.get('mtime'),
                'copied_at': folder.get('done_at') or meta.get('copied_at'),
                'dicom': meta.get('dicom')
            })
        items.sort(key=lambda x: x.get('copied_at') or 0, reverse=True)
        return {'total': len(items), 'items': items[offset:offset + limit]}

    @app.get('/api/v1/log')
    async def get_log(lines: int = 200, _=Depends(verify_api_key)):
        lines = max(1, min(lines, 2000))
        if not os.path.exists(LOG_FILE):
            return {'lines': []}
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-lines:]
        return {'lines': [line.rstrip('\n') for line in tail]}

    @app.websocket('/ws')
    async def ws(websocket: WebSocket):
        cfg = websocket.app.state.web_config
        client = websocket.client.host if websocket.client else ''
        key = websocket.query_params.get('api_key', '')
        trusted = cfg.webTrustLoopback and _is_loopback(client)
        if not trusted and not (cfg.webApiKey and hmac.compare_digest(
                key.encode('utf-8'), cfg.webApiKey.encode('utf-8'))):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json({
                    'type': 'status_update',
                    'data': status_snapshot(websocket.app)
                })
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


def main():
    app = create_app()
    app.state.engine.start()
    cfg = app.state.web_config
    print(f'[RTLC] Web UI: http://{cfg.webHost}:{cfg.webPort}/')
    uvicorn.run(app, host=cfg.webHost, port=cfg.webPort, log_level='warning')


if __name__ == '__main__':
    main()
