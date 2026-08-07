from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_meta(key,value) VALUES('schema_version','2')
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=now();

CREATE TABLE IF NOT EXISTS products (
  nm_id BIGINT PRIMARY KEY,
  vendor_code TEXT,
  name TEXT NOT NULL,
  brand TEXT,
  description TEXT,
  facts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_run_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS interactions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('review','question')),
  nm_id BIGINT REFERENCES products(nm_id),
  text TEXT NOT NULL,
  rating SMALLINT CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
  answered BOOLEAN NOT NULL DEFAULT false,
  answer_text TEXT,
  author_name TEXT,
  created_at_source TIMESTAMPTZ,
  source_run_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  payload_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS drafts (
  id BIGSERIAL PRIMARY KEY,
  interaction_id TEXT NOT NULL REFERENCES interactions(id),
  goal TEXT NOT NULL,
  text TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  risk_json JSONB NOT NULL,
  evidence_json JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  model TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS complaints (
  id BIGSERIAL PRIMARY KEY,
  interaction_id TEXT NOT NULL UNIQUE REFERENCES interactions(id),
  eligible BOOLEAN NOT NULL,
  category TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  reasons_json JSONB NOT NULL,
  evidence_json JSONB NOT NULL,
  questions_json JSONB NOT NULL,
  draft_text TEXT NOT NULL CHECK(length(draft_text) <= 1000),
  status TEXT NOT NULL DEFAULT 'analysis',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  event TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  request_id UUID,
  details_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS access_log (
  id BIGSERIAL PRIMARY KEY,
  request_id UUID NOT NULL UNIQUE,
  actor TEXT NOT NULL,
  authenticated BOOLEAN NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  query_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  status_code INTEGER NOT NULL,
  client_ip INET,
  user_agent TEXT,
  duration_ms INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS interactions_status_idx ON interactions(status,kind);
CREATE INDEX IF NOT EXISTS interactions_nm_idx ON interactions(nm_id);
CREATE INDEX IF NOT EXISTS drafts_interaction_idx ON drafts(interaction_id,created_at DESC);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_log(entity_type,entity_id,created_at DESC);
CREATE INDEX IF NOT EXISTS access_created_idx ON access_log(created_at DESC);
"""


def now() -> datetime:
    return datetime.now(UTC)


def connect(settings: Settings) -> Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=10)


@contextmanager
def transaction(settings: Settings) -> Iterator[Connection]:
    with connect(settings) as conn, conn.transaction():
        yield conn


def init_db(settings: Settings) -> None:
    with transaction(settings) as conn:
        conn.execute(SCHEMA)


def audit(
    conn: Connection,
    event: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any],
    *,
    actor: str = "system",
    request_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log(event,actor,entity_type,entity_id,request_id,details_json)
        VALUES(%s,%s,%s,%s,%s,%s)""",
        (event, actor, entity_type, entity_id, request_id, Jsonb(details)),
    )


def log_access(
    settings: Settings,
    *,
    request_id: str,
    actor: str,
    authenticated: bool,
    method: str,
    path: str,
    query_keys: list[str],
    status_code: int,
    client_ip: str | None,
    user_agent: str | None,
    duration_ms: int,
) -> None:
    with transaction(settings) as conn:
        conn.execute(
            """INSERT INTO access_log(request_id,actor,authenticated,method,path,query_keys,
            status_code,client_ip,user_agent,duration_ms) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(request_id) DO NOTHING""",
            (
                request_id,
                actor,
                authenticated,
                method,
                path,
                Jsonb(query_keys),
                status_code,
                client_ip,
                user_agent,
                duration_ms,
            ),
        )
