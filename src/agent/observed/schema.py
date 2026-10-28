"""Additive registry tables; never alter or delete user diagnostic/chat rows."""
DDL='''CREATE TABLE IF NOT EXISTS agent_observed_systems(
tenant TEXT NOT NULL,observed_id TEXT NOT NULL,name TEXT NOT NULL,
created_at timestamptz NOT NULL DEFAULT now(),registered_at timestamptz,last_registered_at timestamptz,
active boolean NOT NULL DEFAULT true,revision bigint NOT NULL DEFAULT 1,
PRIMARY KEY(tenant,observed_id),UNIQUE(tenant,name));
CREATE TABLE IF NOT EXISTS agent_observed_grants(
grant_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,observed_id TEXT NOT NULL,
services jsonb NOT NULL,origins jsonb NOT NULL,envs jsonb NOT NULL,
active boolean NOT NULL DEFAULT true,created_at timestamptz NOT NULL DEFAULT now(),
FOREIGN KEY(tenant,observed_id) REFERENCES agent_observed_systems(tenant,observed_id));
CREATE TABLE IF NOT EXISTS agent_observed_services(
tenant TEXT NOT NULL,observed_id TEXT NOT NULL,service TEXT NOT NULL,
active boolean NOT NULL DEFAULT true,registered_at timestamptz NOT NULL DEFAULT now(),
PRIMARY KEY(tenant,observed_id,service),
FOREIGN KEY(tenant,observed_id) REFERENCES agent_observed_systems(tenant,observed_id));
CREATE TABLE IF NOT EXISTS agent_observed_instances(
tenant TEXT NOT NULL,observed_id TEXT NOT NULL,service TEXT NOT NULL,instance TEXT NOT NULL,
boot_id TEXT NOT NULL,grant_id TEXT NOT NULL,env TEXT NOT NULL,cluster TEXT,
base_url TEXT NOT NULL,sdk_version TEXT NOT NULL,capabilities jsonb NOT NULL,
registered_at timestamptz NOT NULL DEFAULT now(),last_registered_at timestamptz NOT NULL DEFAULT now(),
revision bigint NOT NULL DEFAULT 1,active boolean NOT NULL DEFAULT true,
PRIMARY KEY(tenant,observed_id,service,instance,boot_id),
FOREIGN KEY(tenant,observed_id,service) REFERENCES agent_observed_services(tenant,observed_id,service),
FOREIGN KEY(grant_id) REFERENCES agent_observed_grants(grant_id));
CREATE INDEX IF NOT EXISTS observed_instance_scope ON agent_observed_instances
(tenant,observed_id,service,env,cluster,active);
ALTER TABLE agent_observed_grants ADD COLUMN IF NOT EXISTS source_types jsonb NOT NULL DEFAULT
'["sdk_http","sdk_sql","sdk_pool","sdk_worker","sdk_runtime","sdk_lifecycle","sdk_deadline","sdk_shutdown","sdk_log","sdk_trace","sdk_transaction","sdk_lock"]'::jsonb;
CREATE TABLE IF NOT EXISTS agent_observed_audit(
seq bigserial PRIMARY KEY,tenant TEXT NOT NULL,observed_id TEXT NOT NULL,action TEXT NOT NULL,
details jsonb NOT NULL,occurred_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE agent_observed_instances ADD COLUMN IF NOT EXISTS endpoints jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE agent_observed_instances ADD COLUMN IF NOT EXISTS business_url text;
ALTER TABLE agent_observed_instances ADD COLUMN IF NOT EXISTS endpoint_revision text NOT NULL DEFAULT '';
ALTER TABLE agent_observed_instances ADD COLUMN IF NOT EXISTS endpoint_status text NOT NULL DEFAULT 'legacy_unavailable';'''
