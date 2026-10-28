"""Bounded durable telemetry queue. Never used from a business request."""
import json
from pathlib import Path
import sqlite3
from diagnostics.priority import priority


class Outbox:
    def __init__(self,path,max_events=512,max_bytes=2*1024*1024):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=.2,check_same_thread=False)
        self.db.execute('PRAGMA max_page_count=2048')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,payload TEXT,size INTEGER,priority INTEGER DEFAULT 0)')
        if 'priority' not in [r[1] for r in self.db.execute('PRAGMA table_info(events)')]:
            self.db.execute('ALTER TABLE events ADD COLUMN priority INTEGER DEFAULT 0')
        self.max_events=max_events;self.max_bytes=max_bytes;self.dropped=0

    def put(self,rows):
        with self.db:
            for row in rows:
                payload=json.dumps(row,ensure_ascii=False,allow_nan=False)
                self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                                (row['event_id'],payload,len(payload.encode()),priority(row)))
            while True:
                count,size=self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM events').fetchone()
                if count<=self.max_events and size<=self.max_bytes:break
                self.db.execute('DELETE FROM events WHERE rowid=(SELECT rowid FROM events ORDER BY priority,rowid LIMIT 1)')
                self.dropped+=1

    def batch(self,limit=32):
        rows=[];size=0
        for r in self.db.execute('SELECT payload FROM events ORDER BY priority DESC,rowid LIMIT ?',
                                 (max(1,min(32,limit)),)):
            count=len(r[0].encode())
            if rows and size+count>220000:break
            rows.append(json.loads(r[0]));size+=count
        return rows

    def ack(self,ids):
        with self.db:self.db.executemany('DELETE FROM events WHERE id=?',[(i,) for i in ids])

    def close(self):self.db.close()
