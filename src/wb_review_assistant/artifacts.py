from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def store_artifact(
    root: Path,
    *,
    source: str,
    entity: str,
    method_id: str,
    docs_url: str,
    request_url: str,
    params: dict[str, Any],
    status: int,
    content_type: str,
    body: bytes,
) -> str:
    stamp = datetime.now(UTC)
    run_id = stamp.strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(4)
    folder = root / source / entity / stamp.strftime("%Y/%m/%d") / run_id
    folder.mkdir(parents=True, exist_ok=False)
    suffix = ".json" if "json" in content_type else ".bin"
    data_name = "response" + suffix
    (folder / data_name).write_bytes(body)
    manifest = {
        "run_id": run_id,
        "source": source,
        "entity": entity,
        "requested_at": stamp.isoformat(),
        "status": "success" if status < 400 else "error",
        "http_status": status,
        "request_url": request_url,
        "request_params": params,
        "content_type": content_type,
        "data_file": data_name,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "method_id": method_id,
        "docs_url": docs_url,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
    )
    return run_id
