DDL='''CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS agent_knowledge_documents(
 tenant text NOT NULL,id text NOT NULL,title text NOT NULL,content text NOT NULL,
 uploader text NOT NULL,category text NOT NULL,tags jsonb NOT NULL,observed_id text,
 status text NOT NULL CHECK(status IN ('review','active','inactive','deleted')),
 revision text NOT NULL,version integer NOT NULL,index_state text NOT NULL DEFAULT 'pending',
 index_error text NOT NULL DEFAULT '',index_model text NOT NULL DEFAULT '',index_chunker text NOT NULL DEFAULT '',
 chunk_count integer NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS knowledge_directory ON agent_knowledge_documents(tenant,updated_at DESC,id);
CREATE TABLE IF NOT EXISTS agent_knowledge_versions(
 tenant text NOT NULL,document_id text NOT NULL,version integer NOT NULL,body jsonb NOT NULL,
 PRIMARY KEY(tenant,document_id,version));
CREATE TABLE IF NOT EXISTS agent_knowledge_chunks(
 tenant text NOT NULL,document_id text NOT NULL,version integer NOT NULL,id text NOT NULL,
 parent_id text NOT NULL,ordinal integer NOT NULL,text text NOT NULL,parent_text text NOT NULL,
 metadata jsonb NOT NULL,model text NOT NULL,embedding vector NOT NULL,terms tsvector NOT NULL,
 PRIMARY KEY(tenant,document_id,version,id));
CREATE INDEX IF NOT EXISTS knowledge_text ON agent_knowledge_chunks USING gin(terms);
CREATE INDEX IF NOT EXISTS knowledge_chunks_scope ON agent_knowledge_chunks(tenant,document_id,version);
CREATE TABLE IF NOT EXISTS agent_knowledge_embeddings(
 tenant text NOT NULL,model text NOT NULL,text_hash text NOT NULL,embedding vector NOT NULL,
 PRIMARY KEY(tenant,model,text_hash));'''
