from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

from .db import audit, now, transaction
from .llm import generate
from .policy import assess_complaint, validate_answer
from .settings import Settings
from .wb import WBClient


def _items(envelope: dict, kind: str) -> list[dict]:
    return (envelope.get("data") or {}).get(kind) or []


def sync_all(settings: Settings) -> dict[str, int]:
    client = WBClient(settings)
    counts = {"products": 0, "reviews": 0, "questions": 0, "archive": 0}
    try:
        for page in client.product_pages():
            with transaction(settings) as conn:
                for card in page.data.get("cards") or []:
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
        for endpoint, db_kind, key in (
            ("feedbacks", "review", "reviews"),
            ("questions", "question", "questions"),
        ):
            for answered in (False, True):
                for page in client.interaction_pages(endpoint, answered):
                    counts[key] += _save_interactions(
                        settings, _items(page.data, endpoint), db_kind, page.run_id
                    )
        for page in client.archived_feedback_pages():
            counts["archive"] += _save_interactions(
                settings, _items(page.data, "feedbacks"), "review", page.run_id, "archived"
            )
    finally:
        client.close()
    return counts


def _save_interactions(
    settings: Settings, items: list[dict], kind: str, run_id: str, status: str = "new"
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
                    status,
                    Jsonb(item),
                    now(),
                ),
            )
        audit(conn, "sync_page", kind, run_id, {"count": len(items)})
    return len(items)


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
