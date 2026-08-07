import json

from wb_review_assistant.artifacts import store_artifact


def test_artifact_is_versioned_and_has_provenance(tmp_path):
    run1 = store_artifact(
        tmp_path,
        source="seller_api",
        entity="feedbacks",
        method_id="wb.feedbacks.list",
        docs_url="https://example.test/docs",
        request_url="https://example.test/api",
        params={"take": 1},
        status=200,
        content_type="application/json",
        body=b'{"data":[]}',
    )
    run2 = store_artifact(
        tmp_path,
        source="seller_api",
        entity="feedbacks",
        method_id="wb.feedbacks.list",
        docs_url="https://example.test/docs",
        request_url="https://example.test/api",
        params={"take": 1},
        status=200,
        content_type="application/json",
        body=b'{"data":[]}',
    )
    assert run1 != run2
    manifests = list(tmp_path.rglob("manifest.json"))
    assert len(manifests) == 2
    manifest = json.loads(manifests[0].read_text())
    assert manifest["sha256"] and manifest["http_status"] == 200
