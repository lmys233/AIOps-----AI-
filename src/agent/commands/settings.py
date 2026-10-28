from ..observed.store import RegistryStore


class Settings(RegistryStore):
    ddl='''CREATE TABLE IF NOT EXISTS agent_chat_commands(
      tenant text NOT NULL,name text NOT NULL,enabled boolean NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(tenant,name));'''

    async def enabled(self,tenant,name):
        row=await self.one('SELECT enabled FROM agent_chat_commands WHERE tenant=$1 AND name=$2',tenant,name)
        return row is None or row['enabled']

    async def set(self,tenant,name,value):
        await self.fetch('''INSERT INTO agent_chat_commands(tenant,name,enabled) VALUES($1,$2,$3)
          ON CONFLICT(tenant,name) DO UPDATE SET enabled=$3,updated_at=now() RETURNING name''',tenant,name,value)


class LocalSettings:
    def __init__(self):self.rows={}
    async def enabled(self,tenant,name):return self.rows.get((tenant,name),True)
    async def set(self,tenant,name,value):self.rows[(tenant,name)]=value
    async def close(self):pass
