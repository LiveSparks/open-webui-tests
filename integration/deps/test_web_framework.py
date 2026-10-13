"""Dependency smoke: FastAPI, through the requests every client of Open WebUI makes.

Every route is FastAPI's: `Depends` hands it the signed-in account, which the auth dependency reads
from an `HTTPBearer` header or, failing that, the `token` cookie, and an `HTTPException` turns into
the status and detail a client sees. Query, path, form and JSON body values are validated into their
declared types (the 422 that names the one that failed is pinned in
integration/deps/test_request_bodies.py), a declared response model trims what a route returns to
the fields it lists, and `UploadFile` with a `Form` field takes an upload whose processing a
`BackgroundTasks` runs after the answer. Chat completions stream as a `StreamingResponse`, a
provider's error comes back as a `PlainTextResponse` or `JSONResponse`, Open WebUI's own middleware
stamps every answer and redirects legacy links, `CORSMiddleware` answers other origins,
`StaticFiles` serves the static directory and, in development, `/docs` is FastAPI's Swagger page
with Open WebUI's bundled assets. The terminal WebSocket route is driven in
integration/security/test_terminal_connection_gating.py and a SCIM `Header` with its
`WWW-Authenticate` answer here. The development instance and the SCIM instance are env-only, so
those tests boot instances of their own.

Discriminates: passes on dev ef67cc3fa; in a backend copy, an `HTTPBearer` with `auto_error=True`
refuses the cookie session, the user search without its response model leaks another account's
settings, background tasks that never run leave the upload pending, a stream sent under a JSON
content type fails the stream test, a verify handler that answers the provider's text as JSON
fails the text case, a `swagger_ui_html` override that is never installed serves FastAPI's CDN
assets on `/docs`, dropping `CORSMiddleware` fails the origin test and a SCIM 401 without its
`WWW-Authenticate` header fails the SCIM test.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from harness import upstream as reply
from harness.scim import SCIM_ENV, scim_client
from harness.upstream import MOCK_MODEL_ID

pytestmark = [pytest.mark.depcheck, pytest.mark.api, pytest.mark.requires_source]

ALLOWED_ORIGIN = "https://owui.example"
DEVELOPMENT = {"ENV": "dev", "CORS_ALLOW_ORIGIN": ALLOWED_ORIGIN}
PROCESSING_WAIT = 30.0


def _anonymous(instance) -> httpx.Client:
    return httpx.Client(base_url=instance.base_url, timeout=60.0, follow_redirects=False)


# ---------------------------------------------------------------- sessions and errors


def test_a_request_without_a_session_is_refused_with_its_reason(instance):
    with _anonymous(instance) as client:
        refused = client.get("/api/v1/auths/")

    assert refused.status_code == 401, refused.text
    assert refused.json() == {"detail": "Not authenticated"}


def test_the_session_is_read_from_the_bearer_header_or_the_token_cookie(instance, make_user):
    account = make_user()
    with _anonymous(instance) as client:
        by_header = client.get(
            "/api/v1/auths/", headers={"Authorization": f"Bearer {account.token}"}
        )
    with httpx.Client(base_url=instance.base_url, cookies={"token": account.token}) as browser:
        by_cookie = browser.get("/api/v1/auths/")

    assert by_header.status_code == 200, by_header.text
    assert by_header.json()["email"] == account.email
    assert by_cookie.status_code == 200, by_cookie.text
    assert by_cookie.json()["email"] == account.email


# ---------------------------------------------------------------- response models


def test_the_user_search_answers_only_the_fields_its_response_model_lists(make_user):
    searched_for = make_user(name=f"Heron {uuid.uuid4().hex[:8]}")
    secret = f"private system prompt {uuid.uuid4().hex}"
    with searched_for.client() as client:
        saved = client.post("/api/v1/users/user/settings/update", json={"ui": {"system": secret}})
    assert saved.status_code == 200, saved.text

    with make_user().client() as client:
        found = client.get("/api/v1/users/search", params={"query": searched_for.name})

    assert found.status_code == 200, found.text
    [listed] = found.json()["users"]
    assert listed["id"] == searched_for.id
    assert "settings" not in listed and "oauth" not in listed, sorted(listed)
    assert secret not in found.text


# ---------------------------------------------------------------- uploads


def test_an_upload_keeps_its_form_metadata_and_is_processed_after_the_answer(make_user):
    with make_user().client() as client:
        uploaded = client.post(
            "/api/v1/files/",
            params={"process": "true", "process_in_background": "true"},
            data={"metadata": json.dumps({"source": "harbour log"})},
            files={
                "file": ("log.txt", b"The harbour lighthouse budget was approved.", "text/plain")
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        answered = uploaded.json()
        deadline = time.monotonic() + PROCESSING_WAIT
        stored = client.get(f"/api/v1/files/{answered['id']}").json()
        while stored["data"].get("status") == "pending" and time.monotonic() < deadline:
            time.sleep(0.2)
            stored = client.get(f"/api/v1/files/{answered['id']}").json()

    assert answered["meta"]["data"] == {"source": "harbour log"}
    assert answered["data"] == {"status": "pending"}
    assert stored["data"]["status"] == "completed", stored["data"]
    assert "lighthouse budget" in stored["data"]["content"]


def test_upload_metadata_that_is_not_json_is_refused(make_user):
    with make_user().client() as client:
        refused = client.post(
            "/api/v1/files/",
            data={"metadata": "{not json"},
            files={"file": ("log.txt", b"text", "text/plain")},
        )

    assert refused.status_code == 400, refused.text
    assert "Invalid metadata format" in refused.json()["detail"]


# ---------------------------------------------------------------- responses


def test_a_chat_completion_streams_as_server_sent_events(make_user, upstream):
    upstream.queue(reply.text(["Harbour ", "lights"]))
    with make_user().client() as client:
        streamed = client.post(
            "/api/chat/completions",
            json={
                "model": MOCK_MODEL_ID,
                "stream": True,
                "messages": [{"role": "user", "content": "what is lit?"}],
            },
        )

    assert streamed.status_code == 200, streamed.text
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = [line.removeprefix("data: ") for line in streamed.text.splitlines() if line]
    assert events[-1] == "[DONE]", events[-3:]
    deltas = [json.loads(event)["choices"][0]["delta"].get("content", "") for event in events[:-1]]
    assert "".join(deltas) == "Harbour lights"


@pytest.mark.parametrize(
    ("answer", "content_type", "shown"),
    [
        pytest.param(b"the provider is down", "text/plain", "the provider is down", id="text"),
        pytest.param(
            b'{"error": "overloaded"}', "application/json", '{"error":"overloaded"}', id="json"
        ),
    ],
)
def test_a_provider_error_is_passed_on_as_it_came(admin, listener, answer, content_type, shown):
    listener.route("GET", "/models", (503, {"Content-Type": content_type}, answer))
    with admin.client() as client:
        verified = client.post("/openai/verify", json={"url": listener.base_url, "key": "sk-test"})

    assert verified.status_code == 503, verified.text
    assert verified.headers["content-type"].startswith(content_type)
    assert verified.text.replace(" ", "") == shown.replace(" ", "")


def test_every_answer_is_stamped_and_a_legacy_video_link_redirects(instance):
    with _anonymous(instance) as client:
        answered = client.get("/api/version")
        redirected = client.get("/watch", params={"v": "dQw4w9WgXcQ"})

    assert float(answered.headers["x-process-time"]) >= 0
    assert redirected.status_code == 307, redirected.text
    assert redirected.headers["location"] == "/?youtube=dQw4w9WgXcQ"


def test_a_file_in_the_static_directory_is_served(instance):
    name = f"depcheck-{uuid.uuid4().hex}.txt"
    static_file = instance.data_dir.parent / "static" / name
    static_file.write_text("served as is")
    try:
        with _anonymous(instance) as client:
            served = client.get(f"/static/{name}")
            missing = client.get(f"/static/missing-{name}")
    finally:
        static_file.unlink()

    assert served.status_code == 200, served.text
    assert served.text == "served as is"
    assert served.headers["content-type"].startswith("text/plain")
    assert missing.status_code == 404


# ---------------------------------------------------------------- env-only settings


@pytest.fixture
def development(instance_with):
    return instance_with(DEVELOPMENT)


@pytest.mark.slow
def test_the_api_docs_use_the_bundled_swagger_assets(development):
    with _anonymous(development) as client:
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

    assert docs.status_code == 200, docs.text
    assert "/static/swagger-ui/swagger-ui-bundle.js" in docs.text
    assert "cdn.jsdelivr.net" not in docs.text
    assert schema.status_code == 200, schema.text
    assert "/api/v1/auths/signin" in schema.json()["paths"]


@pytest.mark.slow
def test_only_the_allowed_origin_is_let_through(development):
    with _anonymous(development) as client:
        allowed = client.get("/api/config", headers={"Origin": ALLOWED_ORIGIN})
        preflight = client.options(
            "/api/v1/auths/signin",
            headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST"},
        )
        foreign = client.get("/api/config", headers={"Origin": "https://elsewhere.example"})

    assert allowed.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert allowed.headers.get("access-control-allow-credentials") == "true"
    assert preflight.status_code == 200, preflight.text
    assert "POST" in preflight.headers["access-control-allow-methods"]
    assert "access-control-allow-origin" not in foreign.headers


@pytest.mark.slow
def test_scim_asks_for_its_bearer_token(instance_with):
    scim = instance_with(SCIM_ENV)
    with _anonymous(scim) as client:
        refused = client.get("/api/v1/scim/v2/Users")
    with scim_client(scim) as client:
        listed = client.get("/Users")

    assert refused.status_code == 401, refused.text
    assert refused.headers.get("www-authenticate") == "Bearer"
    assert listed.status_code == 200, listed.text
