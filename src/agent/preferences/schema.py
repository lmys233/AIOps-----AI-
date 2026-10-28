"""Additive PostgreSQL schema. Published versions and feedback receipts are retained."""
DDL='''
CREATE TABLE IF NOT EXISTS agent_preference_settings(key text PRIMARY KEY,value text NOT NULL);
CREATE TABLE IF NOT EXISTS agent_feedback_latest(
 tenant text,subject text,feedback_id text NOT NULL,observed_id text NOT NULL,session_id text NOT NULL,
 turn_id text NOT NULL,answer_ref text NOT NULL,incident_id text NOT NULL,revision bigint NOT NULL,
 data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,subject,feedback_id),UNIQUE(tenant,subject,observed_id,session_id,turn_id));
CREATE TABLE IF NOT EXISTS agent_feedback_events(
 tenant text,subject text,request_id text,fingerprint text NOT NULL,feedback_id text NOT NULL,
 revision bigint NOT NULL,data jsonb NOT NULL,created_at timestamptz DEFAULT now(),
 PRIMARY KEY(tenant,subject,request_id));
CREATE TABLE IF NOT EXISTS agent_preference_jobs(
 tenant text,subject text,feedback_id text,revision bigint,status text NOT NULL DEFAULT 'queued',
 attempts int NOT NULL DEFAULT 0,lease_until timestamptz,token text,error text,
 created_at timestamptz DEFAULT now(),PRIMARY KEY(tenant,subject,feedback_id,revision));
CREATE INDEX IF NOT EXISTS preference_jobs_ready ON agent_preference_jobs(status,created_at);
CREATE TABLE IF NOT EXISTS agent_preference_journal(
 seq bigserial PRIMARY KEY,tenant text,subject text,feedback_id text,revision bigint,
 content jsonb NOT NULL,tokens int NOT NULL,skipped text,created_at timestamptz DEFAULT now(),
 UNIQUE(tenant,subject,feedback_id,revision));
CREATE INDEX IF NOT EXISTS preference_journal_subject ON agent_preference_journal(tenant,subject,seq);
CREATE TABLE IF NOT EXISTS agent_summary_records(
 id text PRIMARY KEY,tenant text,subject text,purpose text NOT NULL,record jsonb NOT NULL,created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS agent_preference_heads(
 tenant text,subject text,version bigint NOT NULL DEFAULT 0,cursor bigint NOT NULL DEFAULT 0,
 enabled boolean NOT NULL DEFAULT true,learning boolean NOT NULL DEFAULT true,
 lease_until timestamptz,lease_token text,last_checked timestamptz,last_success timestamptz,
 PRIMARY KEY(tenant,subject));
CREATE TABLE IF NOT EXISTS agent_preference_versions(
 tenant text,subject text,version bigint,hash text NOT NULL,profile jsonb NOT NULL,
 cutoff bigint NOT NULL,build_id text NOT NULL,created_at timestamptz DEFAULT now(),
 PRIMARY KEY(tenant,subject,version));
CREATE TABLE IF NOT EXISTS agent_preference_builds(
 build_id text PRIMARY KEY,tenant text,subject text,base_version bigint,cutoff bigint,status text,
 details jsonb NOT NULL DEFAULT '{}',created_at timestamptz DEFAULT now(),finished_at timestamptz);
CREATE TABLE IF NOT EXISTS agent_preference_budget(
 tenant text,subject text,day date,calls int NOT NULL DEFAULT 0,tokens bigint NOT NULL DEFAULT 0,
 wall_ms bigint NOT NULL DEFAULT 0,PRIMARY KEY(tenant,subject,day));
'''
