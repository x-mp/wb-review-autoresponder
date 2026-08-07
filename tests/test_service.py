import os

import pytest

from wb_review_assistant.cli import seed_demo
from wb_review_assistant.db import connect, init_db
from wb_review_assistant.service import analyze_complaint, generate_draft
from wb_review_assistant.settings import Settings


def settings(tmp_path):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return Settings(
        app_access_token="x" * 32,
        database_url=database_url,
        artifact_root=tmp_path / "raw",
    )


def test_demo_generation_complaint_and_audit(tmp_path):
    cfg = settings(tmp_path)
    init_db(cfg)
    with connect(cfg) as conn:
        conn.execute(
            "TRUNCATE access_log,audit_log,complaints,drafts,interactions,products RESTART IDENTITY CASCADE"
        )
        conn.commit()
    seed_demo(cfg)
    draft_id = generate_draft(cfg, "demo-question-1", actor="test-operator", request_id=None)
    result = analyze_complaint(cfg, "demo-review-2", actor="test-operator")
    with connect(cfg) as conn:
        draft = conn.execute("SELECT status FROM drafts WHERE id=%s", (draft_id,)).fetchone()
        events = conn.execute("SELECT count(*) total FROM audit_log").fetchone()["total"]
    assert draft["status"] in {"validated", "needs_human"}
    assert result["category"] == "delivery_only_not_rating"
    assert events >= 3
