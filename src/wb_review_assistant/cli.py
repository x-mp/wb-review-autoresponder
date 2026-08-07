from __future__ import annotations

import argparse
import json
import logging
import time
import uuid

from psycopg.types.json import Jsonb

from .db import audit, init_db, now, transaction
from .service import sync_all
from .settings import get_settings

logger = logging.getLogger(__name__)


def seed_demo(settings) -> None:
    init_db(settings)
    run_id = "demo-" + uuid.uuid4().hex[:12]
    with transaction(settings) as conn:
        conn.execute(
            """INSERT INTO products(nm_id,name,brand,description,facts_json,source_run_id,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(nm_id) DO UPDATE SET name=excluded.name,
            brand=excluded.brand,description=excluded.description,facts_json=excluded.facts_json,
            source_run_id=excluded.source_run_id,updated_at=excluded.updated_at""",
            (
                12345678,
                "Термокружка",
                "Demo",
                "Тестовый товар",
                Jsonb({"объём": "450 мл", "уход": "ручная мойка"}),
                run_id,
                now(),
            ),
        )
        samples = [
            ("demo-review-1", "review", "Кружка удобная, хорошо держит тепло", 5),
            ("demo-review-2", "review", "Курьер опоздал, ужасный ПВЗ", 1),
            ("demo-question-1", "question", "Можно ли мыть в посудомоечной машине?", None),
        ]
        for external_id, kind, text, rating in samples:
            conn.execute(
                """INSERT INTO interactions(id,kind,nm_id,text,rating,answered,source_run_id,status,payload_json,synced_at)
                VALUES(%s,%s,%s,%s,%s,false,%s,'new',%s,%s) ON CONFLICT(id) DO UPDATE SET
                text=excluded.text,rating=excluded.rating,source_run_id=excluded.source_run_id,
                payload_json=excluded.payload_json,synced_at=excluded.synced_at""",
                (external_id, kind, 12345678, text, rating, run_id, Jsonb({}), now()),
            )
        audit(conn, "demo_seeded", "run", run_id, {"count": len(samples)})


def main() -> None:
    parser = argparse.ArgumentParser(description="WB reviews assistant")
    parser.add_argument("command", choices=["init-db", "seed-demo", "sync", "worker"])
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    if args.command == "init-db":
        init_db(settings)
    elif args.command == "seed-demo":
        seed_demo(settings)
    elif args.command == "sync":
        init_db(settings)
        print(json.dumps(sync_all(settings), ensure_ascii=False))
    else:
        init_db(settings)
        while True:
            try:
                logger.info("sync result: %s", sync_all(settings))
            except Exception:
                logger.exception("sync failed")
            time.sleep(settings.sync_interval_seconds)


if __name__ == "__main__":
    main()
