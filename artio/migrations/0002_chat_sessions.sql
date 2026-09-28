CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
ALTER TABLE batches ADD COLUMN session_id INTEGER REFERENCES chat_sessions(id) ON DELETE SET NULL;
ALTER TABLE batches ADD COLUMN message TEXT;
CREATE INDEX batches_session ON batches(session_id, id);
