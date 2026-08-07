from __future__ import annotations

import uuid
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from .db import audit, connect, now, transaction
from .llm import generate
from .policy import assess_complaint, validate_answer
from .settings import Settings
from .wb import WBClient


def _items(envelope: dict, kind: str) -> list[dict]:
    return (envelope.get("data") or {}).get(kind) or []


SYNC_LOCK_ID = 8_214_907_001
DRAFT_LOCK_ID = 8_214_907_002
DEFAULT_RUNTIME_OPTIONS = {
    "automation_enabled": False,
    "sync_interval_seconds": 3600,
    "require_approval": True,
}


def get_runtime_options(settings: Settings) -> dict[str, Any]:
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT value_json FROM sync_state WHERE key='runtime_options'"
        ).fetchone()
    stored = dict(row["value_json"]) if row else {}
    return {**DEFAULT_RUNTIME_OPTIONS, **stored, "require_approval": True}


def update_runtime_options(
    settings: Settings,
    *,
    automation_enabled: bool,
    sync_interval_seconds: int,
    actor: str,
    request_id: str | None,
) -> dict[str, Any]:
    if not 300 <= sync_interval_seconds <= 86_400:
        raise ValueError("Интервал должен быть от 300 до 86400 секунд")
    options = {
        "automation_enabled": automation_enabled,
        "sync_interval_seconds": sync_interval_seconds,
        "require_approval": True,
    }
    with transaction(settings) as conn:
        conn.execute(
            """INSERT INTO sync_state(key,value_json,updated_at)
            VALUES('runtime_options',%s,%s)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
            updated_at=excluded.updated_at""",
            (Jsonb(options), now()),
        )
        audit(
            conn,
            "runtime_options_updated",
            "settings",
            "runtime_options",
            options,
            actor=actor,
            request_id=request_id,
        )
    return options


def sync_all(
    settings: Settings, *, actor: str = "system", request_id: str | None = None
) -> dict[str, int]:
    with connect(settings) as lock_conn:
        acquired = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) acquired", (SYNC_LOCK_ID,)
        ).fetchone()["acquired"]
        if not acquired:
            raise RuntimeError("WB synchronization is already running")
        try:
            return _sync_all(settings, actor=actor, request_id=request_id)
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_ID,))


def _sync_all(settings: Settings, *, actor: str, request_id: str | None) -> dict[str, int]:
    client = WBClient(settings)
    counts = {"products": 0, "reviews": 0, "questions": 0, "archive": 0}
    try:
        for page in client.product_pages():
            cards = page.data.get("cards") or []
            with transaction(settings) as conn:
                for card in cards:
                    facts = {
                        item.get("name"): item.get("value")
                        for item in card.get("characteristics") or []
                        if item.get("name")
                    }
                    conn.execute(
                        """INSERT INTO products(nm_id,vendor_code,name,brand,description,facts_json,source_run_id,updated_at)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(nm_id) DO UPDATE SET vendor_code=excluded.vendor_code,
                           name=excluded.name,brand=excluded.brand,description=excluded.description,
                           facts_json=excluded.facts_json,source_run_id=excluded.source_run_id,updated_at=excluded.updated_at""",
                        (
                            card["nmID"],
                            card.get("vendorCode"),
                            card.get("title") or f"Товар {card['nmID']}",
                            card.get("brand"),
                            card.get("description"),
                            Jsonb(facts),
                            page.run_id,
                            now(),
                        ),
                    )
                    counts["products"] += 1
                audit(
                    conn,
                    "sync_page",
                    "product",
                    page.run_id,
                    {"count": len(cards)},
                    actor=actor,
                    request_id=request_id,
                )
        for endpoint, db_kind, key in (
            ("feedbacks", "review", "reviews"),
            ("questions", "question", "questions"),
        ):
            for answered in (False, True):
                for page in client.interaction_pages(endpoint, answered):
                    counts[key] += _save_interactions(
                        settings,
                        _items(page.data, endpoint),
                        db_kind,
                        page.run_id,
                        actor=actor,
                        request_id=request_id,
                    )
        for page in client.archived_feedback_pages():
            counts["archive"] += _save_interactions(
                settings,
                _items(page.data, "feedbacks"),
                "review",
                page.run_id,
                "archived",
                actor=actor,
                request_id=request_id,
            )
    finally:
        client.close()
    with transaction(settings) as conn:
        conn.execute(
            """INSERT INTO sync_state(key,value_json,updated_at) VALUES('last_sync',%s,%s)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
            updated_at=excluded.updated_at""",
            (Jsonb({"status": "ok", "counts": counts}), now()),
        )
        audit(
            conn,
            "sync_completed",
            "sync",
            "wb",
            counts,
            actor=actor,
            request_id=request_id,
        )
    return counts


def _save_interactions(
    settings: Settings,
    items: list[dict],
    kind: str,
    run_id: str,
    status: str = "new",
    *,
    actor: str = "system",
    request_id: str | None = None,
) -> int:
    with transaction(settings) as conn:
        for item in items:
            external_id = str(item.get("id") or item.get("feedbackId") or uuid.uuid4())
            product = item.get("productDetails") or {}
            nm_id = (
                item.get("nmId") or item.get("nmID") or product.get("nmId") or product.get("nmID")
            )
            if nm_id:
                conn.execute(
                    """INSERT INTO products(nm_id,name,facts_json,source_run_id,updated_at)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(nm_id) DO NOTHING""",
                    (
                        nm_id,
                        product.get("productName") or f"Товар {nm_id}",
                        Jsonb({}),
                        run_id,
                        now(),
                    ),
                )
            text = item.get("text") or item.get("question") or ""
            answer = item.get("answer")
            answer_text = answer.get("text") if isinstance(answer, dict) else answer
            effective_status = status if status == "archived" else (
                "answered" if answer_text else "new"
            )
            conn.execute(
                """INSERT INTO interactions(id,kind,nm_id,text,rating,answered,answer_text,author_name,
                   created_at_source,source_run_id,status,payload_json,synced_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET text=excluded.text,rating=excluded.rating,answered=excluded.answered,
                   answer_text=excluded.answer_text,status=excluded.status,payload_json=excluded.payload_json,
                   source_run_id=excluded.source_run_id,synced_at=excluded.synced_at""",
                (
                    external_id,
                    kind,
                    nm_id,
                    text,
                    item.get("productValuation"),
                    bool(answer_text),
                    answer_text,
                    item.get("userName"),
                    item.get("createdDate") or item.get("createdAt"),
                    run_id,
                    effective_status,
                    Jsonb(item),
                    now(),
                ),
            )
        audit(
            conn,
            "sync_page",
            kind,
            run_id,
            {"count": len(items)},
            actor=actor,
            request_id=request_id,
        )
    return len(items)


def generate_unanswered_review_drafts(
    settings: Settings, *, actor: str = "operator", request_id: str | None = None
) -> dict[str, int]:
    with connect(settings) as lock_conn:
        acquired = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) acquired", (DRAFT_LOCK_ID,)
        ).fetchone()["acquired"]
        if not acquired:
            raise RuntimeError("Bulk draft generation is already running")
        try:
            rows = lock_conn.execute(
                """SELECT i.id FROM interactions i
                WHERE i.kind='review' AND i.answered=false AND i.status <> 'archived'
                AND NOT EXISTS (
                    SELECT 1 FROM drafts d WHERE d.interaction_id=i.id
                )
                ORDER BY COALESCE(i.created_at_source,i.synced_at) DESC"""
            ).fetchall()
            created = 0
            failed = 0
            for row in rows:
                try:
                    generate_draft(
                        settings,
                        row["id"],
                        actor=actor,
                        request_id=request_id,
                    )
                    created += 1
                except (httpx.HTTPError, KeyError, RuntimeError, ValueError):
                    failed += 1
            with transaction(settings) as conn:
                audit(
                    conn,
                    "bulk_drafts_completed",
                    "review",
                    "unanswered",
                    {"selected": len(rows), "created": created, "failed": failed},
                    actor=actor,
                    request_id=request_id,
                )
            return {"selected": len(rows), "created": created, "failed": failed}
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (DRAFT_LOCK_ID,))


def generate_draft(
    settings: Settings, interaction_id: str, *, actor: str = "system", request_id: str | None = None
) -> int:
    with transaction(settings) as conn:
        row = conn.execute("SELECT * FROM interactions WHERE id=%s", (interaction_id,)).fetchone()
        if not row:
            raise KeyError(interaction_id)
        interaction = dict(row)
        product_row = (
            conn.execute("SELECT * FROM products WHERE nm_id=%s", (row["nm_id"],)).fetchone()
            if row["nm_id"]
            else None
        )
        product = dict(product_row) if product_row else None
        if product:
            product["facts"] = product.pop("facts_json")
    candidate, model = generate(settings, interaction, product)
    errors = validate_answer(candidate["answer_text"])
    risks = list(candidate.get("risk_flags") or []) + errors
    status = "needs_human" if risks or candidate.get("confidence", 0) < 0.85 else "validated"
    with transaction(settings) as conn:
        cur = conn.execute(
            """INSERT INTO drafts(interaction_id,goal,text,confidence,risk_json,evidence_json,status,model,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                interaction_id,
                candidate["goal"],
                candidate["answer_text"],
                candidate["confidence"],
                Jsonb(risks),
                Jsonb(candidate.get("evidence_keys") or []),
                status,
                model,
                now(),
            ),
        )
        draft_id = cur.fetchone()["id"]
        audit(
            conn,
            "draft_generated",
            "draft",
            str(draft_id),
            {"model": model, "status": status},
            actor=actor,
            request_id=request_id,
        )
    return int(draft_id)


def analyze_complaint(
    settings: Settings, interaction_id: str, *, actor: str = "system", request_id: str | None = None
) -> dict[str, Any]:
    with transaction(settings) as conn:
        row = conn.execute(
            "SELECT * FROM interactions WHERE id=%s AND kind='review'", (interaction_id,)
        ).fetchone()
        if not row:
            raise KeyError(interaction_id)
        result = assess_complaint(row["id"], row["nm_id"], row["text"])
        data = result.dict()
        conn.execute(
            """INSERT INTO complaints(interaction_id,eligible,category,confidence,reasons_json,evidence_json,
               questions_json,draft_text,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(interaction_id)
               DO UPDATE SET eligible=excluded.eligible,category=excluded.category,confidence=excluded.confidence,
               reasons_json=excluded.reasons_json,evidence_json=excluded.evidence_json,
               questions_json=excluded.questions_json,draft_text=excluded.draft_text,
               created_at=excluded.created_at,updated_at=now()""",
            (
                interaction_id,
                result.eligible,
                result.category,
                result.confidence,
                Jsonb(result.reasons),
                Jsonb(result.evidence),
                Jsonb(result.questions),
                result.draft_text,
                now(),
            ),
        )
        audit(
            conn,
            "complaint_analyzed",
            "interaction",
            interaction_id,
            {"eligible": result.eligible, "category": result.category},
            actor=actor,
            request_id=request_id,
        )
    return data


def approve_draft(
    settings: Settings, draft_id: int, *, actor: str = "operator", request_id: str | None = None
) -> None:
    with transaction(settings) as conn:
        row = conn.execute(
            "SELECT status FROM drafts WHERE id=%s FOR UPDATE", (draft_id,)
        ).fetchone()
        if not row or row["status"] not in {"validated", "needs_human"}:
            raise ValueError("Draft cannot be approved")
        conn.execute(
            "UPDATE drafts SET status='approved',approved_at=%s WHERE id=%s", (now(), draft_id)
        )
        audit(
            conn, "draft_approved", "draft", str(draft_id), {}, actor=actor, request_id=request_id
        )


def publish_draft(
    settings: Settings, draft_id: int, *, actor: str = "operator", request_id: str | None = None
) -> str:
    if not settings.auto_publish:
        raise PermissionError("AUTO_PUBLISH=false; live write is disabled")
    with transaction(settings) as conn:
        row = conn.execute(
            """SELECT d.*,i.kind FROM drafts d JOIN interactions i ON i.id=d.interaction_id
            WHERE d.id=%s FOR UPDATE OF d""",
            (draft_id,),
        ).fetchone()
        if not row or row["status"] != "approved":
            raise ValueError("Only an approved draft may be published")
        errors = validate_answer(row["text"])
        if errors:
            raise ValueError(f"Policy validation failed: {errors}")
    client = WBClient(settings)
    try:
        before = client.get_interaction(row["kind"], row["interaction_id"])
        source = before.data.get("data") or {}
        source_id = str(source.get("id") or source.get("feedbackId") or "")
        if source_id != row["interaction_id"]:
            raise RuntimeError("Read-before identity mismatch; publish aborted")
        result = client.publish_answer(row["kind"], row["interaction_id"], row["text"])
        client.get_interaction(row["kind"], row["interaction_id"])
    finally:
        client.close()
    with transaction(settings) as conn:
        conn.execute(
            "UPDATE drafts SET status='published',published_at=%s WHERE id=%s", (now(), draft_id)
        )
        audit(
            conn,
            "draft_published",
            "draft",
            str(draft_id),
            {"run_id": result.run_id},
            actor=actor,
            request_id=request_id,
        )
    return result.run_id
