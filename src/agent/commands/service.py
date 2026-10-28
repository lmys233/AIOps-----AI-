import os,asyncio
from .registry import CommandRegistry
from .builtins import definitions
from .settings import Settings,LocalSettings
from ..alerts.query import store_of


class Commands:
    def __init__(self,service):
        self.service=service;self.tenant=os.getenv('AIOPS_TENANT','default')
        self.warnings=store_of(service);dsn=getattr(self.warnings.receipts,'dsn',None)
        self.settings=Settings(dsn) if dsn else LocalSettings()
        self.registry=CommandRegistry();self.plugins={d.name:d for d in definitions()};self.lock=asyncio.Lock()
        self.preview_lock=asyncio.Semaphore(2)
        from .suggestions import Suggestions
        self.suggestions=Suggestions(self.warnings)

    async def close(self):
        await self.suggestions.close();await self.settings.close()

    async def enabled(self,name):return name in self.plugins and await self.settings.enabled(self.tenant,name)

    async def sync(self):
        async with self.lock:
            for name,definition in self.plugins.items():
                active=await self.enabled(name)
                if active and name not in self.registry.entries:self.registry.register(definition)
                elif not active:self.registry.destroy(name)
        return self.registry.catalog()

    async def set(self,name,value):
        if name not in self.plugins:raise ValueError('仅能启用本机已安装的可信命令插件')
        await self.settings.set(self.tenant,name,value)
        await self.sync()
        return dict(name=name,enabled=value)


def engine_of(service):
    if getattr(service,'commands',None) is None:service.commands=Commands(service)
    return service.commands
