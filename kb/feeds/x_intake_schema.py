"""Durable X discovery, completion outbox and phase telemetry (additive only)."""

SCHEMA = '''
CREATE TABLE IF NOT EXISTS x_intake_sources (
 source_id INTEGER PRIMARY KEY, identity TEXT NOT NULL, channel TEXT NOT NULL,
 floor TEXT NOT NULL, window_end TEXT NOT NULL DEFAULT '', latest TEXT NOT NULL DEFAULT '', covered TEXT NOT NULL DEFAULT '',
 coverage_end TEXT NOT NULL DEFAULT '',
 discovery TEXT NOT NULL DEFAULT 'unattempted', gap TEXT NOT NULL DEFAULT '',
 scan_at REAL NOT NULL DEFAULT 0, history_at REAL NOT NULL DEFAULT 0,
 expired INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', outside TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS x_intake_candidates (
 post_id TEXT PRIMARY KEY, entry TEXT NOT NULL, published TEXT NOT NULL,
 state TEXT NOT NULL, attempted REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS x_intake_links (
 source_id INTEGER NOT NULL, identity TEXT NOT NULL, post_id TEXT NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(source_id,identity,post_id)
);
CREATE INDEX IF NOT EXISTS x_intake_links_post ON x_intake_links(post_id);
CREATE TABLE IF NOT EXISTS x_intake_batches (
 id TEXT PRIMARY KEY, source_id INTEGER NOT NULL, identity TEXT NOT NULL,
 boundary TEXT NOT NULL, end TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS x_intake_batches_source ON x_intake_batches(source_id,identity,state);
CREATE TABLE IF NOT EXISTS x_intake_batch_posts (
 batch_id TEXT NOT NULL, post_id TEXT NOT NULL, PRIMARY KEY(batch_id,post_id)
);
CREATE TABLE IF NOT EXISTS x_intake_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, metrics TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS x_intake_expirations (
 source_id INTEGER NOT NULL, identity TEXT NOT NULL, post_id TEXT NOT NULL, expired_at TEXT NOT NULL,
 PRIMARY KEY(source_id,identity,post_id)
);
CREATE INDEX IF NOT EXISTS x_intake_expirations_at ON x_intake_expirations(expired_at);
'''
