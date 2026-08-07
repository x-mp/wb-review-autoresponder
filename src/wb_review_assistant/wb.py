from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from .artifacts import store_artifact
from .settings import Settings

DOCS_COMM = "https://dev.wildberries.ru/ru/openapi/user-communication"
DOCS_PRODUCTS = "https://dev.wildberries.ru/ru/openapi/work-with-products"


@dataclass
class Captured:
    data: dict[str, Any]
    run_id: str


class WBClient:
    def __init__(self, settings: Settings):
        if not settings.wb_api_token:
            raise RuntimeError("WB_API_TOKEN is required")
        self.settings = settings
        self.http = httpx.Client(
            headers={
                "Authorization": settings.wb_api_token,
                "User-Agent": "wb-review-assistant/0.1",
            },
            timeout=30,
        )
        self._last_request = 0.0

    def close(self) -> None:
        self.http.close()

    def _capture(
        self,
        method: str,
        url: str,
        *,
        method_id: str,
        entity: str,
        docs_url: str,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> Captured:
        delay = 0.34 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        response = self.http.request(method, url, params=params, json=payload)
        self._last_request = time.monotonic()
        safe_params = params or ({"body_keys": sorted(payload)} if payload else {})
        run_id = store_artifact(
            self.settings.artifact_root,
            source="seller_api",
            entity=entity,
            method_id=method_id,
            docs_url=docs_url,
            request_url=str(response.request.url),
            params=safe_params,
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            body=response.content,
        )
        response.raise_for_status()
        return Captured(response.json() if response.content else {}, run_id)

    def interaction_pages(self, kind: str, answered: bool, take: int = 5000) -> Iterator[Captured]:
        if kind not in {"questions", "feedbacks"}:
            raise ValueError("kind must be questions or feedbacks")
        skip = 0
        while True:
            page = self._capture(
                "GET",
                f"{self.settings.wb_api_base}/api/v1/{kind}",
                method_id=f"wb.{kind}.list",
                entity=kind,
                docs_url=DOCS_COMM,
                params={
                    "isAnswered": str(answered).lower(),
                    "take": take,
                    "skip": skip,
                    "order": "dateDesc",
                },
            )
            yield page
            items = (page.data.get("data") or {}).get(kind) or []
            if len(items) < take or skip + take >= 10000:
                return
            skip += take

    def archived_feedback_pages(self, take: int = 5000) -> Iterator[Captured]:
        skip = 0
        while True:
            page = self._capture(
                "GET",
                f"{self.settings.wb_api_base}/api/v1/feedbacks/archive",
                method_id="wb.feedbacks.archive",
                entity="feedbacks_archive",
                docs_url=DOCS_COMM,
                params={"take": take, "skip": skip, "order": "dateDesc"},
            )
            yield page
            items = (page.data.get("data") or {}).get("feedbacks") or []
            if len(items) < take or skip + take >= 10000:
                return
            skip += take

    def product_pages(self, limit: int = 100) -> Iterator[Captured]:
        cursor: dict[str, Any] = {"limit": limit}
        while True:
            page = self._capture(
                "POST",
                f"{self.settings.wb_content_api_base}/content/v2/get/cards/list",
                method_id="wb.products.list",
                entity="products",
                docs_url=DOCS_PRODUCTS,
                payload={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
            )
            yield page
            cards = page.data.get("cards") or []
            next_cursor = page.data.get("cursor") or {}
            if not cards or next_cursor.get("total", len(cards)) < limit:
                return
            cursor = {
                "limit": limit,
                "updatedAt": next_cursor.get("updatedAt"),
                "nmID": next_cursor.get("nmID"),
            }

    def get_interaction(self, kind: str, external_id: str) -> Captured:
        path = "question" if kind == "question" else "feedback"
        return self._capture(
            "GET",
            f"{self.settings.wb_api_base}/api/v1/{path}",
            method_id=f"wb.{path}.get",
            entity=path,
            docs_url=DOCS_COMM,
            params={"id": external_id},
        )

    def publish_answer(self, kind: str, external_id: str, text: str) -> Captured:
        if not 2 <= len(text) <= 5000:
            raise ValueError("Answer length must be between 2 and 5000 characters")
        if kind == "question":
            return self._capture(
                "PATCH",
                f"{self.settings.wb_api_base}/api/v1/questions",
                method_id="wb.question.answer",
                entity="question_answer",
                docs_url=DOCS_COMM,
                payload={"id": external_id, "answer": {"text": text}, "state": "wbRu"},
            )
        return self._capture(
            "POST",
            f"{self.settings.wb_api_base}/api/v1/feedbacks/answer",
            method_id="wb.feedback.answer.create",
            entity="feedback_answer",
            docs_url=DOCS_COMM,
            payload={"id": external_id, "text": text},
        )
