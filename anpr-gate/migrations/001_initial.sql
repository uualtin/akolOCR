CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_single_admin_idx ON users ((TRUE));

CREATE TABLE IF NOT EXISTS access_entries (
    id BIGSERIAL PRIMARY KEY,
    plate TEXT NOT NULL UNIQUE,
    owner_label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS access_entries_active_plate_idx
    ON access_entries (plate) WHERE is_active;

CREATE TABLE IF NOT EXISTS gates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('entry', 'exit')),
    driver TEXT NOT NULL DEFAULT 'disabled',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('entry', 'exit')),
    gate_id TEXT NOT NULL REFERENCES gates(id),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gate_event_keys (
    id UUID PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_events (
    id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('automatic', 'manual')),
    camera_id TEXT REFERENCES cameras(id),
    gate_id TEXT NOT NULL REFERENCES gates(id),
    plate TEXT,
    confidence DOUBLE PRECISION,
    trigger_status TEXT NOT NULL CHECK (trigger_status IN ('pending', 'success', 'failed', 'disabled')),
    trigger_duration_ms INTEGER,
    trigger_error TEXT,
    full_snapshot_path TEXT,
    crop_snapshot_path TEXT,
    snapshot_status TEXT NOT NULL DEFAULT 'local'
        CHECK (snapshot_status IN ('none', 'local', 'archived', 'partial', 'expired', 'evicted')),
    requested_by BIGINT REFERENCES users(id),
    manual_reason TEXT,
    client_ip INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

ALTER TABLE gate_events ADD COLUMN IF NOT EXISTS client_ip INET;

CREATE INDEX IF NOT EXISTS gate_events_time_idx ON gate_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS gate_events_plate_time_idx ON gate_events (plate, occurred_at DESC);
CREATE INDEX IF NOT EXISTS gate_events_camera_time_idx ON gate_events (camera_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS gate_events_gate_time_idx ON gate_events (gate_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS gate_events_source_time_idx ON gate_events (source, occurred_at DESC);
CREATE INDEX IF NOT EXISTS gate_events_status_time_idx ON gate_events (trigger_status, occurred_at DESC);

CREATE TABLE IF NOT EXISTS admin_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_id BIGINT REFERENCES users(id),
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    client_ip INET,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_audit_time_idx ON admin_audit (occurred_at DESC);
