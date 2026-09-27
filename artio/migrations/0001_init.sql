CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE batches (id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  model_id TEXT, kind TEXT NOT NULL CHECK (kind IN ('generate','workflow')),
  base_params_json TEXT NOT NULL, count INTEGER NOT NULL CHECK (count BETWEEN 1 AND 8));
CREATE TABLE workflows (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, backend_id TEXT NOT NULL,
  graph_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE jobs (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  model_id TEXT, backend_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('generate','workflow')),
  params_json TEXT NOT NULL, graph_json TEXT NOT NULL,
  workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','submitted','done','failed','cancelled')),
  call_id TEXT, error TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  retry_of INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  created_at REAL NOT NULL, submitted_at REAL, finished_at REAL, duration_s REAL, est_cost_usd REAL,
  CHECK (kind = 'workflow' OR model_id IS NOT NULL));
CREATE INDEX jobs_status_backend ON jobs(status, backend_id);
CREATE INDEX jobs_batch ON jobs(batch_id);
CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
  model_id TEXT, file_png TEXT NOT NULL, file_thumb TEXT NOT NULL, width INTEGER NOT NULL,
  height INTEGER NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, seed INTEGER,
  prompt TEXT NOT NULL DEFAULT '', negative TEXT NOT NULL DEFAULT '',
  starred INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX images_model ON images(model_id, id);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE image_tags (image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY (image_id, tag_id));
CREATE TABLE presets (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, model_id TEXT NOT NULL,
  params_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE backend_state (backend_id TEXT PRIMARY KEY, warm_until REAL, ping_call_id TEXT);
CREATE VIRTUAL TABLE images_fts USING fts5(prompt, negative, tags, tokenize = 'unicode61 remove_diacritics 2');
CREATE TRIGGER images_ai AFTER INSERT ON images BEGIN
  INSERT INTO images_fts(rowid, prompt, negative, tags) VALUES (new.id, new.prompt, new.negative, ''); END;
CREATE TRIGGER images_ad AFTER DELETE ON images BEGIN DELETE FROM images_fts WHERE rowid = old.id; END;
CREATE TRIGGER image_tags_ai AFTER INSERT ON image_tags BEGIN
  UPDATE images_fts SET tags = (SELECT coalesce(group_concat(t.name, ' '), '') FROM image_tags it
    JOIN tags t ON t.id = it.tag_id WHERE it.image_id = new.image_id) WHERE rowid = new.image_id; END;
CREATE TRIGGER image_tags_ad AFTER DELETE ON image_tags BEGIN
  UPDATE images_fts SET tags = (SELECT coalesce(group_concat(t.name, ' '), '') FROM image_tags it
    JOIN tags t ON t.id = it.tag_id WHERE it.image_id = old.image_id) WHERE rowid = old.image_id; END;
