"""aiohttp administration on the application's event loop and inference queue."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from pozor.config import Settings
from pozor.infra.config.service import SettingsService
from pozor.infra.files.prompts import FilePromptStore
from pozor.infra.sqlite import health_repo
from pozor.infra.sqlite.admin_pipeline import AdminPipeline
from pozor.infra.sqlite.admin_repo import AdminData
from pozor.infra.sqlite.commands import set_consent
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.telegram_account import AccountError

PASSWORD_KEY=web.RequestKey('active_password',str)
SIGNING_KEY=web.RequestKey('active_key',bytes)

STATIC = Path(__file__).resolve().parents[2] / 'admin_static'


def create_app(data: AdminData, password: str, *, settings: Settings | None = None,
               pipeline: AdminPipeline | None = None, service: SettingsService | None = None,
               health: Callable[[], Awaitable[dict[str, Any]]] | None = None) -> web.Application:
    if len(password) < 16:
        raise ValueError('ADMIN_PASSWORD должен содержать минимум 16 символов')
    settings = settings or Settings(_env_file=None)
    signing_key = secrets.token_bytes(32)
    failures: dict[str, deque[float]] = defaultdict(deque)

    def session(request: web.Request) -> str | None:
        try:
            stamp, nonce, signature = request.cookies['pozor_admin'].split('.')
            expected = hmac.new(request.get(SIGNING_KEY,signing_key), f'{stamp}.{nonce}'.encode(), 'sha256').hexdigest()
            if 0 <= time.time()-int(stamp) < 28800 and hmac.compare_digest(signature, expected):
                return nonce
        except (KeyError, ValueError):
            pass
        return None

    @web.middleware
    async def security(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        try:
            active_password=await asyncio.to_thread(service.admin_password) if service else password
            request[PASSWORD_KEY]=active_password or ''
            request[SIGNING_KEY]=hmac.new(signing_key,(active_password or '').encode(),'sha256').digest()
            if request.method == 'POST':
                origin = request.headers.get('Origin')
                scheme = 'https' if settings.admin_secure_cookie else 'http'
                if origin and origin != f'{scheme}://{request.host}':
                    raise web.HTTPForbidden()
            if request.path.startswith('/api/') and request.path != '/api/login':
                nonce = session(request)
                if nonce is None:
                    return web.json_response({'error':'authentication_required'}, status=401)
                if request.method == 'POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), nonce):
                    return web.json_response({'error':'csrf_required'}, status=403)
            response = await handler(request)
        except AccountError as exc:
            response = web.json_response({'error':str(exc)}, status=400)
        except sqlite3.Error:
            response = web.json_response({'error':'database_unavailable'}, status=503)
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            response = web.json_response({'error':'invalid_query'}, status=400)
        except LookupError:
            response = web.json_response({'error':'not_found'}, status=404)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, text=exc.reason)
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY', 'Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"})
        return response

    async def body(request: web.Request) -> dict[str, Any]:
        if request.content_type != 'application/json':
            raise ValueError
        result = await request.json()
        if not isinstance(result, dict):
            raise TypeError
        return result

    async def login(request: web.Request) -> web.Response:
        current=await asyncio.to_thread(service.snapshot) if service else settings
        peer = request.remote or 'local'
        window = failures[peer]
        now = time.monotonic()
        while window and window[0] < now-current.admin_login_window_seconds:
            window.popleft()
        if len(window) >= current.admin_login_attempts:
            return web.json_response({'error':'too_many_attempts'}, status=429,
                                     headers={'Retry-After':str(current.admin_login_window_seconds)})
        supplied = (await body(request)).get('password')
        if not isinstance(supplied, str):
            raise TypeError
        active_password=request[PASSWORD_KEY]
        if not active_password or not hmac.compare_digest(supplied.encode(), active_password.encode()):
            window.append(now)
            return web.json_response({'error':'invalid_password'}, status=401)
        window.clear()
        nonce, stamp = secrets.token_hex(24), str(int(time.time()))
        signature = hmac.new(request[SIGNING_KEY], f'{stamp}.{nonce}'.encode(), 'sha256').hexdigest()
        response = web.json_response({'csrf_token':nonce})
        response.set_cookie('pozor_admin', f'{stamp}.{nonce}.{signature}', httponly=True,
            secure=settings.admin_secure_cookie, samesite='Strict', max_age=28800)
        return response

    async def logout(request: web.Request) -> web.Response:
        response = web.json_response({})
        response.del_cookie('pozor_admin', samesite='Strict', secure=settings.admin_secure_cookie, httponly=True)
        return response

    async def static(request: web.Request) -> web.Response:
        filename = {'/':'index.html', '/app.js':'app.js','/pipeline.js':'pipeline.js','/style.css':'style.css'}.get(request.path)
        if filename is None:
            raise web.HTTPNotFound()
        content_type = 'text/html' if filename.endswith('html') else 'text/css' if filename.endswith('css') else 'text/javascript'
        return web.Response(body=await asyncio.to_thread((STATIC/filename).read_bytes), content_type=content_type)

    async def api_read(request: web.Request) -> web.Response:
        if request.path == '/api/session':
            result: Any = {'csrf_token':session(request)}
        elif request.path == '/api/settings':
            if service is None:
                raise LookupError
            result = {'settings':await asyncio.to_thread(service.describe)}
        elif request.path == '/api/metrics':
            result = await asyncio.to_thread(GenerationRepository(data.database).metrics)
        elif request.path == '/api/prompt':
            store = FilePromptStore()
            system = store.load('roast')
            result = {'system':system, 'sha256':hashlib.sha256(system.encode()).hexdigest(),
                      'version':store.version('roast'), 'source':'src/pozor/prompts/roast_system.txt'}
        elif request.path.startswith('/api/telegram/'):
            if pipeline is None:
                raise AccountError('Подключение Telegram недоступно в этом запуске.')
            action = request.path.rsplit('/',1)[-1]
            if action not in {'status','groups','available_groups','members','job'}:
                raise LookupError
            result = await pipeline.dispatch(action, {'chat':request.query['chat']} if action == 'members' else {})
        else:
            result = await asyncio.to_thread(data.read, request.path,
                {key:request.query.getall(key) for key in request.query})
        return web.json_response(result, dumps=lambda value:json.dumps(value, ensure_ascii=False))

    async def api_write(request: web.Request) -> web.Response:
        payload = await body(request)
        if request.path == '/api/settings':
            if service is None:
                raise LookupError
            if payload.get('reset') is True:
                await asyncio.to_thread(service.reset, payload['name'], actor='admin')
            else:
                await asyncio.to_thread(service.set, payload['name'], payload['value'], actor='admin')
            return web.json_response({})
        if request.path == '/api/hooks/exclude':
            if type(payload.get('id')) is not int:
                raise ValueError
            await asyncio.to_thread(health_repo.exclude_hook,data.database,payload['id'])
            return web.json_response({})
        if request.path == '/api/chats/level':
            if type(payload.get('chat')) is not int or type(payload.get('level')) is not int:
                raise ValueError
            await asyncio.to_thread(health_repo.chat_level,data.database,payload['chat'],payload['level'])
            return web.json_response({})
        if request.path == '/api/members/roast':
            if (type(payload.get('chat')) is not int or type(payload.get('user')) is not int
                    or type(payload.get('enabled')) is not bool or not -(2**63) <= payload['chat'] < 2**63
                    or not 0 < payload['user'] < 2**63):
                raise ValueError
            if not data.database.is_file():
                raise sqlite3.OperationalError('missing database')
            accepted = await asyncio.to_thread(set_consent,data.database,payload['chat'],payload['user'],
                                               payload['enabled'],respect_forgetting=True)
            return web.json_response({} if accepted else {'error':'Включите сбор в чате. После /forget_me участник должен сам выполнить /roast_on.'}, status=200 if accepted else 409)
        if request.path.startswith('/api/telegram/'):
            if pipeline is None:
                raise AccountError('Подключение Telegram недоступно в этом запуске.')
            action = request.path.rsplit('/',1)[-1]
            if action not in {'connect','password','disconnect','start','cancel','save','add_chat'}:
                raise LookupError
            return web.json_response(await pipeline.dispatch(action,payload))
        raise LookupError

    async def healthz(request: web.Request) -> web.Response:
        if health:
            result = await health()
        else:
            try:
                version = await asyncio.to_thread(health_repo.schema,data.database)
                result={'ok':True,'database':'ok','schema_version':version,'mode':'admin'}
            except sqlite3.Error:
                result = {'ok':False,'database':'unavailable'}
        return web.json_response(result,status=200 if result.get('ok') else 503)

    app = web.Application(middlewares=[security], client_max_size=16384)
    app.router.add_post('/api/login',login)
    app.router.add_post('/api/logout',logout)
    app.router.add_get('/api/{path:.*}',api_read)
    app.router.add_post('/api/{path:.*}',api_write)
    app.router.add_get('/healthz',healthz)
    app.router.add_get('/{path:.*}',static)
    return app
