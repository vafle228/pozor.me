"""Synchronous test harness only. Production never creates a background event loop."""
import asyncio
import threading

from aiohttp import web

from pozor.admin_pipeline import AdminPipeline
from pozor.presentation.admin.server import create_app


class PipelineHarness(AdminPipeline):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.loop=asyncio.new_event_loop()
        self.thread=threading.Thread(target=self.loop.run_forever,daemon=True)
        self.thread.start()

    def call(self,action,body=None):
        return asyncio.run_coroutine_threadsafe(self.dispatch(action,body or {}),self.loop).result(10)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.aclose(),self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


class ServerHarness:
    def __init__(self,data,password,host='127.0.0.1',port=8090,pipeline=None):
        self.loop=pipeline.loop if pipeline else asyncio.new_event_loop()
        self.owns_loop=pipeline is None
        self.started=threading.Event()
        self.stopped=threading.Event()
        self.app=create_app(data,password,pipeline=pipeline)
        self.runner=web.AppRunner(self.app,access_log=None)
        async def setup():
            await self.runner.setup()
            self.site=web.TCPSite(self.runner,host,port)
            await self.site.start()
            self.server_port=self.site._server.sockets[0].getsockname()[1]
        if self.owns_loop:
            self.loop.run_until_complete(setup())
        else:
            asyncio.run_coroutine_threadsafe(setup(),self.loop).result(10)

    def serve_forever(self):
        self.started.set()
        if self.owns_loop:
            self.loop.run_forever()
        else:
            self.stopped.wait(10)

    def shutdown(self):
        self.started.wait(5)
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(),self.loop).result(10)
        if self.owns_loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.stopped.set()

    def server_close(self):
        pass


make_server=ServerHarness
