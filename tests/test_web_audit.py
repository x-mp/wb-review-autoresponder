import os

import pytest
from fastapi.testclient import TestClient

from wb_review_assistant import web
from wb_review_assistant.db import connect
from wb_review_assistant.settings import Settings


def test_access_is_authenticated_and_audited(tmp_path):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    cfg = Settings(
        app_access_token="a" * 32,
        database_url=database_url,
        artifact_root=tmp_path / "raw",
    )
    web.settings = cfg
    with TestClient(web.app) as client:
        assert client.get("/api/summary").status_code == 401
        assert (
            client.get("/api/summary", headers={"Authorization": "Bearer " + "a" * 32}).status_code
            == 200
        )
    with connect(cfg) as conn:
        rows = list(
            conn.execute(
                "SELECT authenticated,status_code FROM access_log ORDER BY created_at DESC LIMIT 2"
            )
        )
    assert {row["authenticated"] for row in rows} == {True, False}
