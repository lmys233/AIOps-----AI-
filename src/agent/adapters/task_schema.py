DDL='''CREATE TABLE IF NOT EXISTS agent_tasks(
 tenant text NOT NULL,observed_id text NOT NULL,session_id text NOT NULL,task_id text NOT NULL,
 revision bigint NOT NULL,payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,observed_id,task_id));
 CREATE INDEX IF NOT EXISTS agent_tasks_session ON agent_tasks(tenant,observed_id,session_id,updated_at DESC);
 CREATE TABLE IF NOT EXISTS agent_task_sessions(
 tenant text,observed_id text,session_id text,foreground text NOT NULL DEFAULT '',
 revision bigint NOT NULL DEFAULT 0,imported boolean NOT NULL DEFAULT false,
 PRIMARY KEY(tenant,observed_id,session_id));
 CREATE TABLE IF NOT EXISTS agent_task_events(
 tenant text,observed_id text,session_id text,task_id text,event_id text,
 revision bigint NOT NULL,event jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,observed_id,event_id));'''
