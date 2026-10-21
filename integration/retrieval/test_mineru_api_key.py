"""Regression: MinerU in local mode failed every document on a 4.x server started with `--api-key`.

Issue open-webui/open-webui#32836: local mode talks to the V1 API of a MinerU 4.x server, but sent
none of its requests with the API Key from the Documents settings, so a key-protected server
answered 401 to the first upload and nothing was parsed. The key now goes along as a Bearer token
whenever one is set.

MinerU is a local service here that answers the V1 calls the way a 4.x server does, and like the
real one it leaves `/v1/health` open and refuses every other V1 call without the key.

Discriminates: passes with fix fed382957 (branch fix/mineru-v1-api-key) on dev ca8f451cc; the
narrow test fails on dev ca8f451cc (MinerU answers 401 to the upload and the file has no text).
The test without a key passes on both.
"""

from __future__ import annotations

import uuid

import pytest

from harness.listener import ReceivedRequest, json_answer, text_answer
from harness.web_retrieval import RETRIEVAL_CONFIG

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

PARSED = "parsed by mineru"
JOB = {
    "job_id": "job-1",
    "status": "completed",
    "tier": "flash",
    "files": [{"output_files": {"markdown": {"file_id": "markdown-1"}}, "parse": {}}],
}
V1_ANSWERS = [
    ("POST", "/v1/uploads", json_answer({"id": "upload-1"})),
    ("PUT", "/v1/uploads/upload-1/content", json_answer({})),
    ("POST", "/v1/uploads/upload-1/complete", json_answer({"file": {"id": "file-1"}})),
    ("POST", "/v1/parse/jobs", json_answer(JOB)),
    ("GET", "/v1/files/markdown-1/content", text_answer(PARSED, "text/markdown")),
]


def mineru_v1_server(listener, api_key: str) -> None:
    """Route `listener` like a MinerU 4.x server, requiring `api_key` when one is given."""
    listener.route("GET", "/v1/health", json_answer({"status": "ok"}))
    for method, path, answer in V1_ANSWERS:

        def handler(request: ReceivedRequest, answer=answer):
            if api_key and request.headers.get("Authorization") != f"Bearer {api_key}":
                return json_answer({"error": {"code": "invalid_api_key"}}, status=401)
            return answer

        listener.route(method, path, handler)


@pytest.fixture
def use_mineru(admin, preserve, listener):
    """Returns `use(api_key)`: MinerU in local mode on `listener`, with that key set."""
    preserve(RETRIEVAL_CONFIG)
    client = admin.client()

    def use(api_key: str):
        saved = client.post(
            RETRIEVAL_CONFIG[1],
            json={
                "CONTENT_EXTRACTION_ENGINE": "mineru",
                "MINERU_API_MODE": "local",
                "MINERU_API_URL": listener.base_url,
                "MINERU_API_KEY": api_key,
                "MINERU_PARAMS": {"tier": "flash"},
                "MINERU_FILE_EXTENSIONS": ["pdf"],
                "BYPASS_EMBEDDING_AND_RETRIEVAL": True,
            },
        )
        assert saved.status_code == 200, saved.text
        return client

    yield use
    client.close()


def upload_pdf(client) -> dict:
    """Upload and process in the foreground; returns the stored file."""
    uploaded = client.post(
        "/api/v1/files/",
        params={"process": "true", "process_in_background": "false"},
        files={
            "file": (f"report-{uuid.uuid4().hex[:6]}.pdf", b"%PDF-1.4 report", "application/pdf")
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    return client.get(f"/api/v1/files/{uploaded.json()['id']}").json()


def test_a_key_protected_mineru_server_parses_the_document(use_mineru, listener):
    api_key = f"mineru-{uuid.uuid4().hex[:8]}"
    mineru_v1_server(listener, api_key)

    stored = upload_pdf(use_mineru(api_key))

    rejected = [
        request.path
        for request in listener.received
        if request.path != "/v1/health"
        and request.headers.get("Authorization") != f"Bearer {api_key}"
    ]
    assert stored["data"].get("content") == PARSED, (
        f"the document was not parsed; MinerU got calls without the key (#32836): {rejected}"
    )
    assert not rejected


def test_without_a_key_mineru_is_called_without_authorization(use_mineru, listener):
    mineru_v1_server(listener, api_key="")

    stored = upload_pdf(use_mineru(""))

    assert stored["data"].get("content") == PARSED, stored["data"]
    v1_calls = [request for request in listener.received if request.path != "/v1/health"]
    assert v1_calls, "MinerU was never called"
    assert all("Authorization" not in request.headers for request in v1_calls), [
        request.headers for request in v1_calls
    ]
