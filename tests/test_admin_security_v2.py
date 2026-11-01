from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from pozor.config import Settings
from pozor.infra.config.service import SettingsService
from pozor.infra.sqlite.admin_repo import AdminData
from pozor.infra.sqlite.connection import migrate
from pozor.presentation.admin.server import create_app


async def test_csrf_rate_limit_and_runtime_setting_apply(tmp_path):
    database=tmp_path/'admin.sqlite3'
    migrate(database)
    settings=Settings(_env_file=None,database_path=database,admin_password='synthetic-password-123')
    service=SettingsService(settings)
    async with TestClient(TestServer(create_app(AdminData(database),'synthetic-password-123',
        settings=settings,service=service)),cookie_jar=CookieJar(unsafe=True)) as client:
        response=await client.post('/api/login',json={'password':'synthetic-password-123'})
        csrf=(await response.json())['csrf_token']
        assert (await client.post('/api/settings',json={'name':'temperature','value':1.1})).status==403
        response=await client.post('/api/settings',json={'name':'temperature','value':1.1},headers={'X-CSRF-Token':csrf})
        assert response.status==200 and service.snapshot().temperature==1.1
        for _ in range(5):
            assert (await client.post('/api/login',json={'password':'bad'})).status==401
        assert (await client.post('/api/login',json={'password':'bad'})).status==429


async def test_dotenv_password_rotation_invalidates_old_session(tmp_path):
    database=tmp_path/'admin.sqlite3'
    migrate(database)
    dotenv=tmp_path/'.env'
    dotenv.write_text('ADMIN_PASSWORD=synthetic-old-password\n','utf-8')
    settings=Settings(database_path=database)
    service=SettingsService(settings)
    async with TestClient(TestServer(create_app(AdminData(database),'synthetic-old-password',
        settings=settings,service=service)),cookie_jar=CookieJar(unsafe=True)) as client:
        assert (await client.post('/api/login',json={'password':'synthetic-old-password'})).status==200
        assert (await client.get('/api/chats')).status==200
        dotenv.write_text('ADMIN_PASSWORD=synthetic-new-password\n','utf-8')
        assert (await client.get('/api/chats')).status==401
        assert (await client.post('/api/login',json={'password':'synthetic-new-password'})).status==200
