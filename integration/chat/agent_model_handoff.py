"""The per-turn model, key, and header handoff reaches the provider.

PR6 of the pi-engine program (issues 22, 23, 20): every chat message the OWUI
backend resolves the turn's model, its provider expression, its header map,
and its params fresh, and hands the sidecar the resolved envelope. The sidecar
never holds a key: each provider call dials the backend's binding-
authenticated key route, so a rotated key is the next request. A built-in
provider pair is used when pi ships the connection's provider (matched on the
sidecar's catalog, which also carries the thinking ladders), and a synthesized
custom provider entry is registered when it does not.

The provider here is a scripted OpenAI-shaped mock that records every request,
so a test reads back the model id, the Authorization header, the resolved
header map, and the merged body params exactly as the wire carried them.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.instance import LaunchedInstance
from harness.upstream import MockUpstream, serve as serve_upstream

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

# The resolution-rule checks import backend modules at source level; open_webui
# env raises SystemExit on import when WEBUI_SECRET_KEY is unset, and config.py
# writes into DATA_DIR. Same guard unit/conftest.py uses, for this process.
os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key-for-unit-tests')
os.environ.setdefault('DATA_DIR', str(Path(tempfile.gettempdir()) / 'owui-unit-tests' / 'data'))
os.environ.setdefault('STATIC_DIR', str(Path(tempfile.gettempdir()) / 'owui-unit-tests' / 'static'))

SIDECAR_PORT = 8946
LANE_DIR = Path('/tmp/agent_model_handoff-lane')
TESTS_REPO = Path(__file__).resolve().parents[2]

AGENT_MODEL = 'agent-handoff.plain'  # wraps mock-model: a built-in pair
AGENT_THINKER = 'agent-handoff.thinker'  # wraps fake-thinker: the effort ladder
AGENT_SYNTH = 'agent-handoff.synth'  # wraps synth-model: a synthesized provider
SYNTH_MODEL_ID = 'synth-model'
KEY_ROTATED = 'sk-rotated-handoff'


# ---------------------------------------------------------------------------
# Fixtures: the catalog provider, the synth provider, the sidecar, the instance
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def catalog_upstream() -> MockUpstream:
    """The provider the sidecar's models.json points at and OWUI's connection 1 names.

    In the lane this is the built-in pair: the sidecar catalog knows this
    base URL, so the backend resolves the turn to pi's own provider entry.
    """
    upstream, shutdown = serve_upstream()
    upstream.models = ['mock-model', 'fake-thinker']
    try:
        yield upstream
    finally:
        shutdown()


@pytest.fixture(scope='module')
def synth_upstream() -> MockUpstream:
    """The provider no catalog knows: OWUI's connection 2, the synthesized path."""
    upstream, shutdown = serve_upstream()
    upstream.models = [SYNTH_MODEL_ID]
    try:
        yield upstream
    finally:
        shutdown()


def _sidecar_health_ok(path: Path) -> bool:
    try:
        health = json.loads(path.read_text())
    except Exception:
        return False
    return health.get('ok') is True and isinstance(health.get('version'), str)


@pytest.fixture(scope='module')
def sidecar(catalog_upstream: MockUpstream):
    """The sidecar from the checkout under test, pointed at the catalog provider."""
    checkout = Path(os.environ.get('OPEN_WEBUI_CHECKOUT', ''))
    if not checkout.is_dir():
        candidate = TESTS_REPO.parent / 'open-webui'
        assert (candidate / 'sidecar').is_dir(), (
            'set OPEN_WEBUI_CHECKOUT to the open-webui worktree whose sidecar dist the lane builds'
        )
        checkout = candidate
    assert (checkout / 'sidecar/dist/index.js').is_file(), 'build the sidecar dist first (npm run build)'

    LANE_DIR.mkdir(parents=True, exist_ok=True)
    sessions = LANE_DIR / 'agent_sessions'
    sessions.mkdir(exist_ok=True)
    models_json = {
        'providers': {
            'fake': {
                'baseUrl': catalog_upstream.base_url,
                'api': 'openai-completions',
                # The dummy the sidecar falls back to; the real key arrives
                # per call from the backend key route and always overrides it.
                'apiKey': 'lane-dummy',
                'models': [
                    {
                        'id': 'mock-model',
                        'name': 'Mock Model',
                        'input': ['text'],
                        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                        'contextWindow': 32000,
                        'maxTokens': 8192,
                        'reasoning': False,
                    },
                    {
                        'id': 'fake-thinker',
                        'name': 'Fake Thinker',
                        'input': ['text'],
                        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                        'contextWindow': 32000,
                        'maxTokens': 8192,
                        'reasoning': True,
                        'thinkingLevelMap': {
                            'off': 'off',
                            'medium': 'medium',
                            'high': 'high',
                            'xhigh': None,
                            'max': None,
                        },
                        'compat': {'supportsReasoningEffort': True},
                    },
                ],
            }
        }
    }
    agent_dir = LANE_DIR / 'agent_dir'
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / 'models.json').write_text(json.dumps(models_json))
    (agent_dir / 'auth.json').write_text('{}')

    env = {
        **os.environ,
        'AGENT_SIDECAR_PORT': str(SIDECAR_PORT),
        'AGENT_SIDECAR_HOST': '127.0.0.1',
        'AGENT_SIDECAR_AGENT_DIR': str(agent_dir),
        'AGENT_SIDECAR_MODEL': 'fake/mock-model',
        'AGENT_SIDECAR_CWD': str(LANE_DIR),
        'AGENT_SESSIONS': str(sessions),
    }
    log_path = LANE_DIR / 'sidecar.log'
    with open(log_path, 'wb') as log:
        process = subprocess.Popen(
            ['node', str(checkout / 'sidecar/dist/index.js')],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 30
        health = LANE_DIR / 'health.json'
        while time.monotonic() < deadline:
            try:
                httpx.get(f'http://127.0.0.1:{SIDECAR_PORT}/health', timeout=2).raise_for_status()
                body = httpx.get(f'http://127.0.0.1:{SIDECAR_PORT}/health', timeout=2)
                health.write_text(body.text)
                if _sidecar_health_ok(health):
                    break
            except Exception:
                pass
            assert process.is_alive() if hasattr(process, 'is_alive') else process.poll() is None, (
                f'the sidecar died on boot: {log_path.read_text()[-2000:]}'
            )
            time.sleep(0.2)
        else:
            raise AssertionError(f'the sidecar never answered /health: {log_path.read_text()[-2000:]}')
        yield {'port': SIDECAR_PORT}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)


def _publish_preset(client: httpx.Client, model_id: str, name: str, base_model_id: str) -> None:
    created = client.post(
        '/api/v1/models/create',
        json={'id': model_id, 'name': name, 'base_model_id': base_model_id, 'meta': {}, 'params': {}},
    )
    assert created.status_code == 200 or 'already registered' in created.text, created.text
    grant = {'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}
    shared = client.post(
        '/api/v1/models/model/access/update',
        json={'id': model_id, 'name': name, 'access_grants': [grant]},
    )
    assert shared.status_code == 200, shared.text


@pytest.fixture(scope='module')
def handoff_instance(
    instance_with,
    sidecar: dict,
    catalog_upstream: MockUpstream,
    synth_upstream: MockUpstream,
) -> LaunchedInstance:
    """The scratch instance: two connections (catalog + synth), user-info forwarding on."""
    launched = instance_with(
        {
            'AGENT_SIDECAR_PORT': str(sidecar['port']),
            'AGENT_LOOP_MODEL_IDS': f'{AGENT_MODEL},{AGENT_THINKER},{AGENT_SYNTH}',
            'OPENAI_API_BASE_URLS': f'{catalog_upstream.base_url};{synth_upstream.base_url}',
            'OPENAI_API_KEYS': 'sk-mock;sk-two',
            'OPENAI_API_CONFIGS': json.dumps({'0': {'headers': {'X-Handoff-Chat': 'chat-{{CHAT_ID}}'}}}),
            'ENABLE_FORWARD_USER_INFO_HEADERS': 'true',
        }
    )
    with launched.client() as client:
        client.get('/api/models', params={'refresh': 'true'}).raise_for_status()
        _publish_preset(client, AGENT_MODEL, 'Handoff Plain', 'mock-model')
        _publish_preset(client, AGENT_THINKER, 'Handoff Thinker', 'fake-thinker')
        _publish_preset(client, AGENT_SYNTH, 'Handoff Synth', SYNTH_MODEL_ID)
        client.get('/api/models', params={'refresh': 'true'}).raise_for_status()
    return launched


@pytest.fixture(scope='module')
def handoff_admin(handoff_instance: LaunchedInstance):
    return admin_of(handoff_instance)


@pytest.fixture(autouse=True)
def _quiet_providers(catalog_upstream: MockUpstream, synth_upstream: MockUpstream):
    catalog_upstream.reset('ok')
    # reset() rewinds the model list to [mock-model]; put the lane's models back.
    catalog_upstream.models = ['mock-model', 'fake-thinker']
    synth_upstream.reset('ok')
    synth_upstream.models = [SYNTH_MODEL_ID]


def _chat_requests(upstream: MockUpstream) -> list[dict]:
    return [body for body in upstream.chat_requests() if body.get('messages')]


def _sidecar_request(upstream: MockUpstream) -> tuple[dict, dict[str, str]]:
    """The provider request the sidecar's pi sent (its user-agent marks it), lower-cased headers."""
    sidecar = [
        request
        for request in upstream.requests
        if request.path.endswith('/chat/completions') and request.body is not None
        and request.body.get('messages')
        and 'pi (' in str({key.lower(): value for key, value in dict(request.headers).items()}.get('user-agent', ''))
    ]
    assert sidecar, 'the sidecar never dialed the provider'
    last = sidecar[-1]
    return last.body, {key.lower(): value for key, value in dict(last.headers).items()}


def _sidecar_catalog() -> dict:
    response = httpx.get(f'http://127.0.0.1:{SIDECAR_PORT}/catalog', timeout=10)
    response.raise_for_status()
    return response.json()


def _continue(client: httpx.Client, turn, stored: dict, content: str, model: str, **options):
    """Send the next turn of a chat with the history riding the request, as
    the browser sends it, so the bridge counts turns instead of forking."""
    history = [
        {'id': turn.user_message_id, 'role': 'user', 'content': 'first turn'},
        {'id': turn.assistant_message_id, 'role': 'assistant', 'content': message_text(stored)},
    ]
    return send_message(
        client,
        content,
        model=model,
        chat_id=turn.chat_id,
        parent_id=turn.assistant_message_id,
        history=history,
        **options,
    )


def message_text(message: dict) -> str:
    return ''.join(
        part.get('text', '')
        for item in (message.get('output') or [])
        if item.get('type') == 'message'
        for part in item.get('content', [])
    )


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_catalog_serves_pi_ladders(handoff_admin):
    """The sidecar serves each model's supported thinking ladder out of pi's catalogs."""
    catalog = _sidecar_catalog()
    fake = next(provider for provider in catalog['providers'] if provider['id'] == 'fake')
    by_id = {model['id']: model for model in fake['models']}
    assert by_id['mock-model']['thinkingLevels'] == ['off']
    thinker = by_id['fake-thinker']
    assert thinker['reasoning'] is True
    # The explicit map hides xhigh/max; the rest follow pi's ladder.
    assert thinker['thinkingLevels'] == ['off', 'minimal', 'low', 'medium', 'high']


def test_built_in_pair_matches_the_connection(handoff_admin, catalog_upstream):
    """A model pi ships resolves to the built-in pair, and the key came from the key route."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'hello provider', model=AGENT_MODEL)
        wait_for_reply(client, turn)
    requests = _chat_requests(catalog_upstream)
    assert requests, 'the provider never saw a chat completion'
    body = requests[-1]
    assert body['model'] == 'mock-model'
    # The Authorization matches OWUI's connection key, not the sidecar's
    # models.json dummy: the key crossed through the key route at use time.
    _, headers = _sidecar_request(catalog_upstream)
    assert headers['authorization'] == 'Bearer sk-mock'


def test_key_rotation_is_the_next_request(handoff_admin, catalog_upstream):
    """Rotate the connection key mid chat; the next message carries the new key."""
    with handoff_admin.client() as client:
        saved_config = client.get('/openai/config')
        saved_config.raise_for_status()
    try:
        with handoff_admin.client() as client:
            turn = send_message(client, 'before rotation', model=AGENT_MODEL)
            stored = wait_for_reply(client, turn)

            # Rotate the connection's key in the admin row (issue 22: the key
            # resolves fresh every message). Keys live on the openai
            # connection config.
            current = saved_config.json()
            current['OPENAI_API_KEYS'] = [KEY_ROTATED, 'sk-two']
            saved = client.post('/openai/config/update', json=current)
            assert saved.status_code == 200, saved.text
            # The save clears the registry caches; repopulate before the next turn.
            client.get('/api/models', params={'refresh': 'true'}).raise_for_status()

            turn = _continue(client, turn, stored, 'after rotation', AGENT_MODEL)
            wait_for_reply(client, turn)
        _, headers = _sidecar_request(catalog_upstream)
        assert headers['authorization'] == 'Bearer ' + KEY_ROTATED
    finally:
        with handoff_admin.client() as client:
            client.post('/openai/config/update', json=saved_config.json()).raise_for_status()
            client.get('/api/models', params={'refresh': 'true'}).raise_for_status()


def test_synthesized_provider_dials_the_connection(handoff_admin, synth_upstream):
    """A model no catalog knows rides a synthesized entry and keeps its own key."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'hello synth', model=AGENT_SYNTH)
        wait_for_reply(client, turn)
    requests = _chat_requests(synth_upstream)
    assert requests, 'the synthesized provider never saw a chat completion'
    body = requests[-1]
    assert body['model'] == SYNTH_MODEL_ID
    # Per-connection keying (issue 22, 3a): connection 2 keeps its own key.
    _, headers = _sidecar_request(synth_upstream)
    assert headers['authorization'] == 'Bearer sk-two'
    # The catalog never knew this base URL: the sidecar registered the entry.
    catalog = _sidecar_catalog()
    assert any(provider['id'].startswith('owui-synth-') for provider in catalog['providers'])


def test_header_map_resolves_server_side(handoff_admin, catalog_upstream):
    """Custom template headers and user-info forwarding arrive resolved, filled in server-side."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'headers please', model=AGENT_MODEL)
        wait_for_reply(client, turn)
    _, headers = _sidecar_request(catalog_upstream)
    # The {{CHAT_ID}} template was filled in by the backend, not carried raw.
    assert headers.get('x-handoff-chat') == 'chat-' + turn.chat_id
    assert headers.get('x-openwebui-user-id') == handoff_admin.id


def test_effort_level_rides_the_request(handoff_admin, catalog_upstream):
    """The chosen effort lands as the request's reasoning_effort, in the model's dialect."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'think hard', model=AGENT_THINKER, params={'reasoning_effort': 'high'})
        wait_for_reply(client, turn)
    body = _chat_requests(catalog_upstream)[-1]
    assert body['model'] == 'fake-thinker'
    assert body['reasoning_effort'] == 'high'


def test_effort_rounds_to_the_supported_ladder(handoff_admin, catalog_upstream):
    """A level the model cannot support rounds to the nearest supported one (issue 20)."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'think xhigh', model=AGENT_THINKER, params={'reasoning_effort': 'xhigh'})
        wait_for_reply(client, turn)
    body = _chat_requests(catalog_upstream)[-1]
    # xhigh maps to null on this model; pi rounds to the nearest supported level.
    assert body['reasoning_effort'] == 'high'


def test_unset_effort_uses_pi_default(handoff_admin, catalog_upstream):
    """An unset effort stays truly unset: pi's own default applies (issue 20)."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'no effort set', model=AGENT_THINKER)
        wait_for_reply(client, turn)
    body = _chat_requests(catalog_upstream)[-1]
    assert body.get('reasoning_effort') == 'medium'


def test_mid_chat_switch_is_the_next_turn(handoff_admin, catalog_upstream):
    """Change the model mid chat; the next turn uses it and history does not fork."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'first with plain', model=AGENT_MODEL)
        wait_for_reply(client, turn)
        stored = wait_for_reply(client, turn)
        turn = _continue(client, turn, stored, 'now the thinker', AGENT_THINKER)
        wait_for_reply(client, turn)
    requests = _chat_requests(catalog_upstream)
    assert [body['model'] for body in requests[-2:]] == ['mock-model', 'fake-thinker']
    last = requests[-1]
    user_turns = [message for message in last['messages'] if message.get('role') == 'user']
    assert len(user_turns) == 2, 'the switch forked the history'


def test_chat_controls_params_cross(handoff_admin, catalog_upstream):
    """A chat control param crosses into the recorded request (issue 23, Q3)."""
    with handoff_admin.client() as client:
        turn = send_message(client, 'stay cool', model=AGENT_MODEL, params={'temperature': 0.25})
        wait_for_reply(client, turn)
    body = _chat_requests(catalog_upstream)[-1]
    assert body['temperature'] == 0.25


def test_structured_output_param_crosses(handoff_admin, catalog_upstream):
    """A response_format schema rides the payload-rewrite hook into the request."""
    schema = {'type': 'json_object'}
    with handoff_admin.client() as client:
        turn = send_message(client, 'answer in json', model=AGENT_MODEL, params={'response_format': schema})
        wait_for_reply(client, turn)
    body = _chat_requests(catalog_upstream)[-1]
    assert body['response_format'] == schema


def test_key_route_refuses_an_unbound_caller(handoff_instance: LaunchedInstance):
    """The key route is binding-authenticated: no token and a foreign token both 401."""
    base = handoff_instance.base_url
    assert httpx.post(f'{base}/loopback/agent/key', timeout=10).status_code == 401
    forged = httpx.post(
        f'{base}/loopback/agent/key',
        headers={'Authorization': 'Bearer AAAAAA.BBBBBB'},
        timeout=10,
    )
    assert forged.status_code == 401


def test_handoff_resolution_rules(handoff_instance: LaunchedInstance):
    """The pure resolution rules: effort mapping and the attribution override."""
    print('DBG secret env:', repr(os.environ.get('WEBUI_SECRET_KEY')))
    import sys
    print('DBG open_webui already imported:', 'open_webui' in sys.modules)
    from open_webui.routers.agent_runtime import _map_thinking_level, openrouter_attribution_override

    assert _map_thinking_level('none') == 'off'
    assert _map_thinking_level('High') == 'high'
    assert _map_thinking_level('  ') is None
    assert _map_thinking_level('banana') is None
    assert openrouter_attribution_override('https://openrouter.ai/api/v1') == {
        'X-OpenRouter-Title': None,
        'X-OpenRouter-Categories': None,
    }
    assert openrouter_attribution_override('https://example.com/v1') == {}



