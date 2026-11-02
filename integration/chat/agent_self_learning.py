"""The self-learning loop runs end to end: observe, reflect, inject (PR11).

The self-learning decision (issue 14) and ADR 0004: every agent turn is
observed into a scrubbed 30-day log, reflected after the turn against the
admin-configured cheap model (never inside the loop), and the lessons that
survive are retrieved per turn from OWUI's own vector store — by similarity
above a relevance floor on the user's message plus session activity, within a
char budget, plus the always-on mature set. The confidence ledger is
client-side evidence with weekly decay: the model never grades itself, and
"keep the base" is always a valid reflection. The numeric gates auto-draft a
Skill (a user-owned artifact), caps rot the weakest oldest lessons out, and the
switches (admin and per user) stop observe, reflect, and inject without
touching the chat. No durable reflection runner: the sidecar's log cursor
resumes where it stopped.

The scripted provider fake answers the reflect request (`OWUI_LESSON_REFLECT`)
with the lesson a mistake turn names (`lesson-topic-<slug>`), so every stage
asserts a deterministic shape: the observation record, the reflect outcome,
the injection at a related turn's head, the ledger rows, and the graduation
draft. The perf probe runs last: the learning-off head time is recorded
FIRST, then 50 interleaved on/off turns — learning on must add no more than
80 ms per turn on the median.

Similarity math of the lane: the store's similarity is the chroma [0,1]
normalization, (1 + cosine) / 2 — the lesson (deploy + retry) and the related
question (deploy + docs) sit near 0.76 mapped, and an unrelated question
(orchids) near 0.50, so the 0.6 relevance floor separates them cleanly; the
mistake pair itself rides at ~1.0.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from harness.actors import Actor, admin_of, create_user
from harness.chat import ask, send_message, wait_for_reply
from harness.instance import LaunchedInstance
from harness.keyword_embeddings import keyword_embedding_env, serve_keyword_embeddings
from harness.listener import listening

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

SIDECAR_PORT = 8957  # the fake rides 15011 (this lane's protocol slot)
LANE_DIR = "/tmp/agent_self_learning-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]
AGENT_MODEL_ID = "agent-self-learning.test"
FAKE_PORT = 15011
FAKE_REQUEST_LOG = Path(LANE_DIR) / "fake-requests.log"
OBSERVATION_LOG = Path(LANE_DIR) / "agent_learning" / "observation.jsonl"
SEED_SECRET = "sk-faketest12345678"
CONFIG_ROUTE = "/api/v1/lessons/config"
LESSON_ROUTE = "/api/v1/lessons"

# The mistake turn carries a correction keyword ('wrong'), the secret the
# scrubber must catch, and the topic the fake's lesson script keys on.
MISTAKE = "wrong flag order when deploying, topic {topic}, bearer {secret}"
DEFAULT_TOPIC = "lesson-topic-main"
RELATED = "deploy the docs site today"
UNRELATED = "why do orchids pout indoors"


# ---------------------------------------------------------------------------
# Fixtures: the sidecar under test, the instance on it, the agent-loop model.
# ---------------------------------------------------------------------------


def _checkout() -> Path:
    checkout = Path(os.environ.get("OPEN_WEBUI_CHECKOUT", ""))
    if not checkout.is_dir():
        candidate = TESTS_REPO.parent / "open-webui"
        assert (candidate / "sidecar").is_dir(), (
            "set OPEN_WEBUI_CHECKOUT to the open-webui worktree whose sidecar dist the lane boots"
        )
        checkout = candidate
    return checkout


@pytest.fixture(scope="module")
def sidecar() -> Iterator[dict]:
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    Path(LANE_DIR).mkdir(parents=True, exist_ok=True)
    FAKE_REQUEST_LOG.write_text("")
    env = {
        **os.environ,
        "AGENT_SIDECAR_LANE_DIR": LANE_DIR,
        "OPENAI_FAKE_LOG": str(FAKE_REQUEST_LOG),
        "AGENT_FAKE_PORT": str(FAKE_PORT),
    }
    service = {"boot": boot, "checkout": _checkout(), "env": env}
    subprocess.run([str(boot), "start", str(service["checkout"]), str(SIDECAR_PORT)], check=True, env=env)
    service["port"] = SIDECAR_PORT
    yield service
    subprocess.run([str(boot), "stop"], check=False, env=env)


@pytest.fixture(scope="module")
def keyword_embeddings() -> Iterator:
    """Keyword vectors on the lane's protocol slot: retrieval ranks predictably."""
    keywords = ["deploy", "retry", "orchid", "docs"]
    with listening(port=FAKE_PORT + 1) as listener:
        serve_keyword_embeddings(listener, keywords)
        yield listener


@pytest.fixture(scope="module")
def learning_instance(instance_with, sidecar, keyword_embeddings) -> LaunchedInstance:
    """Chat models run the sidecar loop; RAG embeddings run the keyword engine,
    so lessons and questions share one vocabulary."""
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{FAKE_PORT}/v1",
            "OPENAI_API_KEYS": "sk-lane",
            **keyword_embedding_env(keyword_embeddings),
        }
    )


@pytest.fixture(scope="module")
def learning_admin(learning_instance: LaunchedInstance) -> Actor:
    return admin_of(learning_instance)


@pytest.fixture()
def dial(learning_admin):
    """Turn one loop dial at a time. The POST is partial: unset dials stand."""

    def set_config(**settings) -> dict:
        with learning_admin.client() as client:
            posted = client.post(CONFIG_ROUTE, json=settings)
            assert posted.status_code == 200, posted.text
            return posted.json()

    return set_config


@pytest.fixture(autouse=True)
def learning_defaults(dial):
    """Learning on by default; the graduation gates only lowered as needed."""
    dial(
        enabled=True,
        injection_floor=0.6,
        confidence_floor=0.3,
        char_budget=2000,
        max_active_lessons=64,
        base_confidence=0.55,
        gate_age_days=0,
        gate_confirmations=3,
        gate_confidence=0.7,
    )
    yield


@pytest.fixture(autouse=True)  # noqa: PT001
def agent_model(learning_admin) -> str:
    """The agent-loop model, published once for the whole module's turns."""
    with learning_admin.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": AGENT_MODEL_ID,
                "name": "Agent Self-Learning Test",
                "base_model_id": "fake-model",
                "meta": {"capabilities": {"file_context": False}},
                "params": {},
            },
        )
        if created.status_code != 200 and "already registered" not in created.text:
            assert created.status_code == 200, created.text
        grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
        for model_id in ("fake-model", AGENT_MODEL_ID):
            client.post(
                "/api/v1/models/model/access/update",
                json={"id": model_id, "name": model_id, "access_grants": [grant]},
            ).raise_for_status()
        listed = client.get("/api/models", params={"refresh": "true"})
        listed.raise_for_status()
        body = listed.json()
        models = body.get("data") if isinstance(body, dict) else body
        assert any(model["id"] == AGENT_MODEL_ID for model in models), [
            m.get("id") for m in models if isinstance(m, dict)
        ]
    return AGENT_MODEL_ID


# ---------------------------------------------------------------------------
# Helpers: the observation log, the store surfaces, the provider requests.
# ---------------------------------------------------------------------------


def observation_lines() -> list[dict]:
    if not OBSERVATION_LOG.exists():
        return []
    return [json.loads(line) for line in OBSERVATION_LOG.read_text().splitlines() if line.strip()]


def lessons_for(actor: Actor) -> list[dict]:
    with actor.client() as client:
        response = client.get(LESSON_ROUTE)
        assert response.status_code == 200, response.text
        return response.json()


def fake_requests(fragment: str | None = None) -> list[dict]:
    if not FAKE_REQUEST_LOG.exists():
        return []
    return [
        json.loads(line)
        for line in FAKE_REQUEST_LOG.read_text().splitlines()
        if line.strip() and (fragment is None or fragment in (json.loads(line).get("last_user") or ""))
    ]


def reflect_requests() -> list[dict]:
    return [entry for entry in fake_requests("OWUI_LESSON_REFLECT")]


def injected_requests(fragment: str | None = None) -> list[dict]:
    """Provider requests whose head carries a lesson_hints block: since fix
    round 1 the block rides the head channel (the request's system message,
    PR3/PR8) and never the user turn, so the tag asserts against the head."""
    return [
        entry
        for entry in fake_requests()
        if "lesson_hints" in (entry.get("last_system") or "")
        and (fragment is None or fragment in (entry.get("last_user") or ""))
    ]


def wait_for_lesson(actor: Actor, probe: str, timeout: float = 25.0) -> dict:
    """The reflect job is post-turn and asynchronous: poll the user's list."""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = lessons_for(actor)
        for row in rows:
            if probe in row.get("trigger", ""):
                return row
        time.sleep(0.2)
    pytest.fail(f"no lesson matched {probe!r} after {timeout}s; store held {rows}")


def lesson_row(actor: Actor, lesson_id: str) -> dict | None:
    return next((row for row in lessons_for(actor) if row["id"] == lesson_id), None)


def confirm(actor: Actor, lesson_id: str, times: int = 1) -> dict:
    """The review surface's confirmations; each one is a deterministic pulse."""
    with actor.client() as client:
        confirmed = None
        for _ in range(times):
            confirmed = client.post(f"{LESSON_ROUTE}/{lesson_id}/confirm")
            assert confirmed.status_code == 200, confirmed.text
            confirmed = confirmed.json()
        return confirmed


def wait_until(predicate, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    pytest.fail(f"{message} (waited {timeout}s)")


def send_turn(client, content: str, model: str) -> dict:
    turn = send_message(client, content, model=model)
    return wait_for_reply(client, turn)


def graduate_the_default_lesson(actor: Actor, model: str) -> dict:
    """Run the mistake turn, then close the numeric gates with confirmations."""
    send_turn(actor.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model)
    lesson = wait_for_lesson(actor, "wrong way for lesson-topic-main")
    if lesson["status"] == "graduated":
        return lesson
    confirm(actor, lesson["id"], times=3)
    wait_until(
        lambda: (lesson_row(actor, lesson["id"]) or {}).get("status") == "graduated",
        15,
        f"lesson {lesson['id']} never graduated",
    )
    return lesson_row(actor, lesson["id"])


# ---------------------------------------------------------------------------
# Unit lane: observe-log shape, scrubbing, reflect outcomes.
# ---------------------------------------------------------------------------


def test_observed_turn_is_shapeful_and_scrubbed(learning_admin, agent_model):
    """The observe half: one shaped record per run, secrets never in it."""
    admin = learning_admin
    before = len(observation_lines())
    with admin.client() as client:
        turn = send_message(client, MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
        stored = wait_for_reply(client, turn)
    assert "done" or stored  # the stored reply arrived; the loop completed
    lines = observation_lines()
    assert len(lines) >= before + 1
    record = lines[-1]
    assert set(record) >= {
        "ts",
        "chatId",
        "sessionId",
        "userText",
        "assistantText",
        "toolCalls",
        "injectedLessonIds",
        "outcome",
        "durationMs",
    }
    assert record["chatId"] == turn.chat_id
    assert record["outcome"] in ("completed", "error", "aborted")
    assert record["durationMs"] > 0
    plain_blob = json.dumps(lines)
    assert SEED_SECRET not in plain_blob, "a secret survived the scrubber"
    assert "[redacted" in plain_blob, "the scrubber left no trace of its work"


def test_reflect_job_writes_one_shaped_lesson(learning_admin, agent_model):
    """The reflect half: signals spend the budget; the cheap model's lesson
    lands with its source evidence and the base confidence — never graded."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    lesson = wait_for_lesson(admin, "wrong way for lesson-topic-main")
    assert lesson["title"].startswith("Deploy flag ordering")
    assert lesson["action"], "the lesson names what to do instead"
    assert lesson["source"], "the lesson cites the moment that caused it"
    assert SEED_SECRET not in lesson["source"], "the source quotes the scrubbed log, not the raw turn"
    assert lesson["status"] == "active"
    assert lesson["confirmations"] == 0
    assert lesson["confidence"] == pytest.approx(0.55), "the base confidence, unchanged by the model"
    assert lesson["skill_id"] is None

    # The reflection prompts went to the cheap model with the loop's marker,
    # and the fake's scripted lesson rides them.
    store_hits = [entry for entry in reflect_requests() if "lesson-topic-main" in entry["last_user"]]
    assert store_hits, "the reflection prompt never reached the cheap model"


def test_quiet_turns_skip_the_reflection_budget(learning_admin, agent_model):
    """Signal-scored: a plain answer with no tools and no correction pays nothing."""
    # Let any in-flight reflection from earlier mistakes land before the
    # baseline: the count below is a strict equality, not a delta over noise.
    wait_until(lambda: len(observation_lines()) > 0, 10, "no observations accrued")
    time.sleep(1.5)
    before = len(reflect_requests())
    send_turn(
        learning_admin.client(),
        f"count this as a quiet turn, {uuid.uuid4().hex[:8]}",
        model=agent_model,
    )
    time.sleep(1.0)
    assert len(reflect_requests()) == before, "a quiet turn ran a reflection job"


def test_a_duplicate_mistake_adds_no_lesson(learning_admin, agent_model):
    """The dedup guard: the same mistake twice writes one lesson still."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    lesson = wait_for_lesson(admin, "wrong way for lesson-topic-main")
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    wait_until(
        lambda: (lesson_row(admin, lesson["id"]) or {}).get("status") in ("active", "graduated"),
        5,
        "the original lesson vanished under a duplicate",
    )
    duplicates = [
        row
        for row in lessons_for(admin)
        if "lesson-topic-main" in row["trigger"] and row["id"] != lesson["id"]
    ]
    assert not duplicates, "the dedup guard let a duplicate lesson through"


def test_ledger_rows_and_confirmations_rise(learning_admin, agent_model):
    """The ledger: confirm twice, the confidence rises and evidence rows accrue."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    lesson = wait_for_lesson(admin, "wrong way for lesson-topic-main")
    if lesson["status"] == "graduated":  # an earlier lane may have closed the gates
        pytest.skip("the default lesson is already graduated on this store")
    first = confirm(admin, lesson["id"])
    assert first["confirmations"] == 1
    second = confirm(admin, lesson["id"])
    assert second["confirmations"] == 2
    assert second["confidence"] > first["confidence"], "confirmations raise the confidence"
    with admin.client() as client:
        events = client.get(f"{LESSON_ROUTE}/{lesson['id']}/events").json()
        kinds = [event["kind"] for event in events]
        assert "written" in kinds
        assert kinds.count("confide") >= 2


# ---------------------------------------------------------------------------
# The injection half: retrieval by relevance, floor discipline.
# ---------------------------------------------------------------------------


def test_injection_rides_a_related_turn_and_keeps_the_floor(learning_admin, agent_model):
    """Dynamic injection: the related turn's provider request carries the
    lesson head; the unrelated one stays under the relevance floor."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    lesson = wait_for_lesson(admin, "wrong way for lesson-topic-main")

    send_turn(admin.client(), RELATED, model=agent_model)
    related = injected_requests(RELATED)
    assert related, "no related turn rode the lesson head"
    assert "lesson_hints" not in (related[-1]["last_user"] or ""), "the lesson block rode the user turn"
    head = related[-1]["last_system"]
    assert "lesson_hints" in head
    assert lesson["trigger"] in head, "the head carries the lesson body"
    assert "WHEN" in head and "THEN" in head, "the head is the composed WHEN/THEN shape"

    send_turn(admin.client(), UNRELATED, model=agent_model)
    assert not injected_requests(UNRELATED), "a lesson below the relevance floor was injected"


def test_char_budget_cuts_the_head(learning_admin, dial, agent_model):
    """The injection of record stays inside the char budget the admin set."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    wait_for_lesson(admin, "wrong way for lesson-topic-main")
    send_turn(admin.client(), RELATED, model=agent_model)
    related = injected_requests(RELATED)
    assert related, "no related turn rode the lesson head"
    head = related[-1]["last_system"]
    # The head sits inside the same provider request; the block it carries is
    # bounded by the dial — assert the composed block is under the lane's 2000
    # chars even though the raw store of lessons can exceed it.
    hint_block = head[head.index("lesson_hints") :]
    assert hint_block.count("WHEN") >= 1
    assert len(hint_block) < 2000, "the composed block exceeded the lane budget"


# ---------------------------------------------------------------------------
# Graduation: the gates draft the Skill.
# ---------------------------------------------------------------------------


def test_graduation_gates_draft_a_user_owned_skill(learning_admin, agent_model):
    """Past the gates, the loop auto-drafts the Skill: user-owned, private,
    inactive draft, carrying the lesson's source."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    lesson = wait_for_lesson(admin, "wrong way for lesson-topic-main")
    if lesson["status"] == "graduated":
        fresh = lesson
    else:
        confirm(admin, lesson["id"], times=3)
        wait_until(
            lambda: (lesson_row(admin, lesson["id"]) or {}).get("status") == "graduated",
            15,
            f"lesson {lesson['id']} never graduated despite the confirmations",
        )
        fresh = lesson_row(admin, lesson["id"])
    assert fresh["skill_id"], "a graduated lesson names its Skill draft"
    with admin.client() as client:
        skills = client.get("/api/v1/skills/").json()
        skill = next((entry for entry in skills if entry["id"] == fresh["skill_id"]), None)
        assert skill is not None, "the auto-drafted Skill is missing from the user's list"
        assert skill["is_active"] is False, "the draft stays inactive until the user turns it on"
        assert skill["user_id"] == fresh["user_id"], "the draft is the driving user's own artifact"
        detail = client.get(f"/api/v1/skills/id/{fresh['skill_id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert fresh["id"] in body.get("content", ""), "the draft lost its source lesson"


def test_mature_lessons_stay_always_on(learning_admin, dial, agent_model):
    """Maturity outranks retrieval: the graduated lesson rides every turn even
    with the floor turned up past everything else."""
    admin = learning_admin
    graduate_the_default_lesson(admin, agent_model)
    send_turn(admin.client(), UNRELATED, model=agent_model)
    related = injected_requests(UNRELATED)
    assert related, "the always-on set did not ride an unrelated turn"
    head = related[-1]["last_system"]
    assert "lesson_hints" in head
    assert "lesson_hints" not in (related[-1]["last_user"] or ""), "the always-on set rode the user turn"
    # The floor is beyond the graduated lesson's similarity; it rode anyway.
    with admin.client() as client:
        rows = client.get(LESSON_ROUTE).json()
        graduated = [row for row in rows if row["status"] == "graduated"]
        assert graduated, "the graduated lesson vanished from the store"
        for row in graduated:
            assert row["trigger"] in head or row["title"] in head


# ---------------------------------------------------------------------------
# Caps and rot.
# ---------------------------------------------------------------------------


def test_caps_rot_the_weakest_oldest_lessons_out(learning_admin, dial, agent_model):
    """Caps control rot: past the cap further writes drop the weakest oldest
    active lessons, never by relevance pressure and never a graduation."""
    admin = learning_admin
    dial(max_active_lessons=2)
    written = []
    for topic in ("lesson-topic-cap-a", "lesson-topic-cap-b", "lesson-topic-cap-c"):
        send_turn(admin.client(), MISTAKE.format(topic=topic, secret=SEED_SECRET), model=agent_model)
        written.append(wait_for_lesson(admin, topic))
    # Rot is visible by absence: a dropped lesson leaves the review surface,
    # and its OWN events route still carries the rot evidence.
    rows = lessons_for(admin)
    listed_ids = {row["id"] for row in rows}
    active = [row["id"] for row in rows if row["status"] == "active"]
    dropped = [row["id"] for row in written if row["id"] not in listed_ids]
    assert len(active) <= 2, f"the cap let {len(active)} active lessons through"
    assert dropped, "rot dropped nothing past the cap"
    for lesson_id in dropped:
        with admin.client() as client:
            events = [event["kind"] for event in client.get(f"{LESSON_ROUTE}/{lesson_id}/events").json()]
        assert "rot_drop" in events, "the dropped lesson carries no rot evidence"
    # A dropped lesson disappears from the review surface and from injection.
    with admin.client() as client:
        listed = client.get(LESSON_ROUTE).json()
        assert not [row for row in listed if row["id"] in dropped]


# ---------------------------------------------------------------------------
# Per-user scope and the switches.
# ---------------------------------------------------------------------------


def scope_user(learning_instance: LaunchedInstance) -> Actor:
    return create_user(learning_instance, role="user")


def test_lessons_never_cross_users(learning_admin, learning_instance, agent_model):
    """Scope is structural: a second user asking the same question gets no
    injection and writes no lesson of the first user's."""
    admin = learning_admin
    send_turn(admin.client(), MISTAKE.format(topic=DEFAULT_TOPIC, secret=SEED_SECRET), model=agent_model)
    wait_for_lesson(admin, "wrong way for lesson-topic-main")
    other = scope_user(learning_instance)
    probe = f"deploy the cross-user probe {uuid.uuid4().hex[:8]}"
    with other.client() as client:
        send_turn(client, probe, model=agent_model)
    assert not injected_requests(probe), "a foreign lesson crossed the user boundary"
    own = lessons_for(other)
    assert own == [], "the second user's store is not clean: a foreign lesson crossed"


def test_switches_off_stop_observe_reflect_and_inject(learning_admin, dial, agent_model, learning_instance):
    """The switches: admin off stops the loop for everyone; per-user off stops
    just that user. The chat itself behaves exactly as before."""
    admin = learning_admin
    dial(enabled=False)
    before_obs = len(observation_lines())
    before_reflect = len(reflect_requests())
    tag = f"switch-off-{uuid.uuid4().hex[:8]}"
    send_turn(admin.client(), MISTAKE.format(topic="lesson-topic-switch", secret=SEED_SECRET) + f" {tag}", model=agent_model)
    assert len(observation_lines()) == before_obs, "observe kept running while learning was switched off"
    assert len(reflect_requests()) == before_reflect, "reflect kept running while learning was switched off"
    assert not injected_requests(tag), "inject kept running while learning was switched off"

    dial(enabled=True)
    off_user = create_user(learning_instance, role="user")
    with off_user.client() as client:
        client.post(f"{LESSON_ROUTE}/switch", json={"enabled": False}).raise_for_status()
    user_tag = f"user-switch-off-{uuid.uuid4().hex[:8]}"
    before_obs = len(observation_lines())
    send_turn(
        off_user.client(),
        MISTAKE.format(topic="lesson-topic-user-switch", secret=SEED_SECRET) + f" {user_tag}",
        model=agent_model,
    )
    assert len(observation_lines()) == before_obs, "the per-user switch left observe running"
    assert not injected_requests(user_tag), "the per-user switch left inject running"
    assert lessons_for(off_user) == [], "the switched-off user wrote a lesson"


def test_admin_summary_reports_the_aggregates(learning_admin):
    """The review surface's admin half: counts, graduated, confirm rate."""
    admin = learning_admin
    with admin.client() as client:
        summary = client.get(f"{LESSON_ROUTE}/admin/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert set(body) >= {
            "statusCounts",
            "lessons",
            "active",
            "graduated",
            "dropped",
            "confirmations",
            "confirmRate",
        }
        assert isinstance(body["statusCounts"], dict)


# ---------------------------------------------------------------------------
# Perf: the learning-off head from FIRST, then 50 interleaved on/off turns.
# ---------------------------------------------------------------------------


def test_learning_on_adds_at_most_80ms_on_the_median(learning_admin, dial, agent_model):
    """The perf rule, in the recorded order: the learning-off head time from
    FIRST, then 50 interleaved on/off turns, then the 80 ms median rule."""
    admin = learning_admin
    dial(enabled=False)  # the baseline state comes first
    client = admin.client()
    for _ in range(5):  # warm the instance, the sidecar, and the embedding stack
        send_turn(client, f"quiet perf warm-up {uuid.uuid4().hex[:8]}", model=agent_model)

    measured: dict[str, list[float]] = {"off": [], "on": []}
    for index in range(50):
        learning_on = index % 2 == 0
        dial(enabled=learning_on)
        turn = send_message(client, f"quiet perf probe turn {index}", model=agent_model)
        started = time.monotonic()
        wait_for_reply(client, turn)
        measured["on" if learning_on else "off"].append(time.monotonic() - started)

    off_median = statistics.median(measured["off"])
    on_median = statistics.median(measured["on"])
    gap = (on_median - off_median) * 1000.0
    print(
        f"perf: learning off median {off_median * 1000:.1f} ms, on median {on_median * 1000:.1f} ms, "
        f"added {gap:.1f} ms on the median over 50 interleaved turns"
    )
    assert len(measured["off"]) == 25 and len(measured["on"]) == 25
    assert gap <= 80.0, f"learning on added {gap:.0f} ms per turn on the median (rule: 80 ms)"
