"""Regression: web fetch address checks, the Playwright loader's scope, and web loaders that pace.

`1717b49` (v0.11.0) made the web fetch guard pull out the IPv4 address an IPv6 address carries
(IPv4-compatible `::a.b.c.d`, NAT64 `64:ff9b::/96`, mapped, 6to4, teredo) and judge it as well.
v0.10.2 asked `ipaddress.is_global` alone, which CPython 3.11 answers True for `::7f00:1` and
`64:ff9b::7f00:1`, so a URL on either literal passed the check and the instance connected to it.
A URL that cannot be read is a 400 either way, so the tests read the log line naming the
refused address.

`1e0ab8471` (#27528, issue #26079) unshadowed the `time` module in the web loader. `from datetime
import datetime, time, timedelta` made pacing call `datetime.time.sleep`, which raised inside
each URL's `try`, so every paced page after the first was dropped and logged as an SSL
verification failure. Microsoft Web IQ is the engine whose search path runs that pacing.

Public addresses, IPv6 ones embedding a public IPv4 included, still pass the guard: an external
web loader behind web search is handed exactly the URLs the guard let through, so the check is
seen without the instance fetching a public host.

`bef63a2` (v0.11.0) rewrote the Playwright loader's route hooks. v0.10.2 waved through every
request that was not the page itself, and with `AIOHTTP_CLIENT_ALLOW_REDIRECTS` on let the
browser follow a redirect chain unchecked, so a public page could pull an internal target as a
sub-resource or redirect to one. Now the loader fetches every request and every hop itself
through the address checks, and opens the page with service workers and websockets blocked.
The instance drives its own headless Chromium here, through an attached link (the synchronous
loader) and web search (the async one); the internal host is a second local service on
127.0.0.2 that the fetch filter list blocks.

Discriminates: passes on dev ef67cc3fa; with `_embedded_ipv4` returning nothing the four
IPv4-compatible and NAT64 cases fail (no refusal logged), with `time` imported from `datetime`
again the paced search returns one page of two, blocking every address that embeds an IPv4 one
fails the public addresses, and in the Playwright loader continuing sub-resource routes,
continuing a redirected page, opening the page without `service_workers='block'` or dropping the
websocket route each let the internal host be reached, and a hop limit of 200 in the loader or in
the attached-link probe fails the endless redirect chain.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from harness.listener import json_answer, listening, text_answer
from harness.web_retrieval import (
    LOCAL_WEB_FETCH,
    save_web_settings,
    serve_search_results,
    web_settings_restored,
)

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

PROCESS_WEB = "/api/v1/retrieval/process/web"


def read_page(actor, url):
    with actor.client() as client:
        return client.post(PROCESS_WEB, json={"url": url})


@pytest.mark.parametrize(
    ("address", "embedded"),
    [
        ("::7f00:1", "127.0.0.1"),  # IPv4-compatible
        ("::a00:1", "10.0.0.1"),
        ("64:ff9b::7f00:1", "127.0.0.1"),  # NAT64 well-known prefix
        ("64:ff9b::a00:1", "10.0.0.1"),
    ],
)
def test_ipv6_carrying_an_internal_ipv4_is_refused(instance, user, address, embedded):
    log_offset = instance.log_size()

    response = read_page(user, f"http://[{address}]/")

    assert response.status_code == 400
    assert f"Blocked non-global address: {embedded}" in instance.log_since(log_offset), (
        f"http://[{address}]/ was not refused for the {embedded} it carries"
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "[::ffff:127.0.0.1]",  # IPv4-mapped
        "[2002:7f00:1::]",  # 6to4
        "[2001:0:4136:e378:8000:63bf:3fff:fdd2]",  # teredo
        "[64:ff9b:1::7f00:1]",  # NAT64 local-use prefix
        "[::1]",
    ],
)
def test_every_internal_address_spelling_is_refused(instance, user, listener, host):
    listener.route("GET", "/secret", text_answer("internal only"))
    log_offset = instance.log_size()

    response = read_page(user, f"http://{host}:{listener.port}/secret")

    assert response.status_code == 400
    assert listener.received == [], f"{host} reached the local service"
    assert "Blocked" in instance.log_since(log_offset)  # by address or by the default filter list


INTERNAL_HOST = "127.0.0.2"
FETCH_ENV = {
    **LOCAL_WEB_FETCH,
    "WEB_FETCH_FILTER_LIST": f"!{INTERNAL_HOST}",
    "AIOHTTP_CLIENT_ALLOW_REDIRECTS": "true",
}


@pytest.fixture(scope="module")
def fetching_instance(instance_with):
    return instance_with(FETCH_ENV)


def browse_answer(request):
    page_url = request.json()["url"]
    return json_answer({"content": f"text of {page_url}", "url": page_url})


@pytest.mark.parametrize("pages_per_second", [2, 0])
def test_paced_web_search_loads_every_page(fetching_instance, listener, pages_per_second):
    pages = [f"{listener.base_url}/first", f"{listener.base_url}/second"]
    listener.route("POST", "/browse", browse_answer)

    with fetching_instance.client() as client, web_settings_restored(client):
        save_web_settings(
            client,
            **serve_search_results(listener, pages),
            WEB_LOADER_ENGINE="microsoft_web_iq",
            MICROSOFT_WEB_IQ_API_BASE_URL=listener.base_url,
            MICROSOFT_WEB_IQ_API_KEY="web-iq-key",
            WEB_LOADER_CONCURRENT_REQUESTS=pages_per_second,
            BYPASS_WEB_SEARCH_WEB_LOADER=False,
            BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=True,
        )
        log_offset = fetching_instance.log_size()
        response = client.post("/api/v1/retrieval/process/web/search", json={"queries": ["pace"]})

    assert response.status_code == 200, response.text
    loaded = [doc["metadata"]["source"] for doc in response.json()["docs"]]
    assert loaded == pages, f"pacing dropped a page: {loaded}"
    assert "SSL verification failed" not in fetching_instance.log_since(log_offset)


PUBLIC_ADDRESSES = ["93.184.216.34", "[2606:4700:4700::1111]", "[::1.1.1.1]", "[64:ff9b::8.8.8.8]"]


def external_loader_answer(request):
    return json_answer(
        [
            {"page_content": f"text of {url}", "metadata": {"source": url}}
            for url in request.json()["urls"]
        ]
    )


def test_public_addresses_stay_allowed(admin, listener):
    """Nearby: only the internal control is dropped before the loader is handed the URLs."""
    public_urls = [f"http://{address}/" for address in PUBLIC_ADDRESSES]
    listener.route("POST", "/load", external_loader_answer)

    with admin.client() as client, web_settings_restored(client):
        save_web_settings(
            client,
            **serve_search_results(listener, [*public_urls, "http://[::7f00:1]/"]),
            WEB_LOADER_ENGINE="external",
            EXTERNAL_WEB_LOADER_URL=f"{listener.base_url}/load",
            EXTERNAL_WEB_LOADER_API_KEY="loader-key",
            BYPASS_WEB_SEARCH_WEB_LOADER=False,
            BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=True,
        )
        response = client.post(PROCESS_WEB + "/search", json={"queries": ["public"]})

    assert response.status_code == 200, response.text
    handed_over = [
        url for request in listener.requests_to("/load") for url in request.json()["urls"]
    ]
    assert handed_over == public_urls, f"the guard refused a public address: {handed_over}"


# --- The Playwright loader: every request and every hop goes through the checks ---


def chromium_installed() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).exists()


@pytest.fixture
def hosts():
    """A public page host the loader may fetch, and an internal host the filter list blocks."""
    with listening() as page_host, listening(host=INTERNAL_HOST) as internal_host:
        internal_host.route("GET", "/admin", text_answer("internal only"))
        yield page_host, internal_host


@pytest.fixture
def playwright_client(fetching_instance, hosts):
    """An admin client on the fetching instance with the Playwright loader selected."""
    if not chromium_installed():
        pytest.skip(
            "no Chromium for the instance's Playwright loader (playwright install chromium)"
        )
    page_host, _ = hosts
    with fetching_instance.client() as client, web_settings_restored(client):
        save_web_settings(
            client,
            **serve_search_results(page_host, [f"{page_host.base_url}/page"]),
            WEB_LOADER_ENGINE="playwright",
            PLAYWRIGHT_WS_URL="",
            BYPASS_WEB_SEARCH_WEB_LOADER=False,
            BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=True,
        )
        yield client


def load_attached_link(client, url):
    """The synchronous loader, as when a user attaches a link; None when nothing loaded."""
    loaded = client.post(PROCESS_WEB + "?process=false", json={"url": url})
    return loaded.json()["content"] if loaded.status_code == 200 else None


def load_search_result(client, url):
    """The async loader, as behind web search (whose results are the page URL)."""
    searched = client.post(PROCESS_WEB + "/search", json={"queries": ["page"]})
    docs = searched.json().get("docs") if searched.status_code == 200 else None
    return docs[0]["content"] if docs else None


LOADERS = pytest.mark.parametrize(
    "load", [load_attached_link, load_search_result], ids=["attached-link", "web-search"]
)


def is_from_browser(request):
    # Only Chromium sends client hints; the attached-link probe sends the web loader's headers.
    return "sec-ch-ua" in request.headers


def browser_requests(host):
    """What the loader fetched for the browser, without the attached-link probe."""
    return [request for request in host.received if is_from_browser(request)]


SUB_RESOURCES = {
    "script": "<script src='{internal}?script'></script>",
    "stylesheet": "<link rel='stylesheet' href='{internal}?stylesheet'>",
    "xhr": (
        "<script>var request = new XMLHttpRequest();"
        "request.open('GET', '{internal}?xhr'); request.send();</script>"
    ),
    "fetch": "<script>fetch('{internal}?fetch');</script>",
    "iframe": "<iframe src='{internal}?iframe'></iframe>",
}


@pytest.mark.requires_browser
@LOADERS
def test_sub_resources_on_an_internal_host_are_refused(playwright_client, hosts, load):
    """Narrow: a public page cannot make the browser pull an internal target."""
    page_host, internal_host = hosts
    internal = f"{internal_host.base_url}/admin"
    tags = "".join(tag.format(internal=internal) for tag in SUB_RESOURCES.values())
    page_host.route("GET", "/page", text_answer(f"<p>public page</p>{tags}"))

    text = load(playwright_client, f"{page_host.base_url}/page")

    assert text and "public page" in text, f"the page itself did not load: {text!r}"
    reached = [request.path for request in internal_host.received]
    assert reached == [], f"the page's sub-resources reached the internal host: {reached}"


@pytest.mark.requires_browser
def test_public_sub_resources_are_fetched_by_the_loader(playwright_client, hosts):
    """Nearby: an allowed script still runs, fetched by the loader and handed to the page."""
    page_host, _ = hosts
    page = "<p id='out'>not run</p><script src='/app.js'></script>"
    page_host.route("GET", "/page", text_answer(page))
    script = "document.getElementById('out').textContent = 'script ran';"
    page_host.route("GET", "/app.js", text_answer(script, content_type="text/javascript"))

    text = load_attached_link(playwright_client, f"{page_host.base_url}/page")

    assert text and "script ran" in text, f"the public script did not run: {text!r}"


def redirect_to(location):
    return 302, {"Location": location}, b""


@pytest.mark.requires_browser
@LOADERS
def test_a_redirect_hop_to_an_internal_host_is_refused(playwright_client, hosts, load):
    page_host, internal_host = hosts
    page_host.route("GET", "/page", redirect_to(f"{internal_host.base_url}/admin"))

    text = load(playwright_client, f"{page_host.base_url}/page")

    assert internal_host.received == [], "the redirect reached the internal host"
    assert not text or "internal only" not in text


@pytest.mark.requires_browser
def test_a_redirect_chain_is_followed_hop_by_hop(playwright_client, hosts):
    """Nearby: a public redirect still lands on its page, each hop fetched by the loader."""
    page_host, _ = hosts
    page_host.route("GET", "/go", redirect_to("/page"))
    page_host.route("GET", "/page", text_answer("<p>public page</p>"))

    text = load_attached_link(playwright_client, f"{page_host.base_url}/go")

    assert text and "public page" in text, f"the redirect was not followed: {text!r}"
    assert [request.path for request in browser_requests(page_host)] == ["/go", "/page"]


@pytest.mark.requires_browser
def test_an_endless_redirect_chain_is_abandoned(playwright_client, hosts):
    page_host, _ = hosts
    page_host.route("GET", "/loop", redirect_to("/loop"))

    text = load_attached_link(playwright_client, f"{page_host.base_url}/loop")

    assert not text
    assert len(browser_requests(page_host)) <= 25, "the loader kept following the loop"
    probed = [request for request in page_host.received if not is_from_browser(request)]
    assert len(probed) <= 31, f"the attached-link probe kept following the loop: {len(probed)}"


def slow_script(_request):
    time.sleep(2)  # holds the page open while the service worker would install
    return text_answer("", content_type="text/javascript")


@pytest.mark.requires_browser
@LOADERS
def test_a_service_worker_cannot_reach_an_internal_host(playwright_client, hosts, load):
    """A service worker's own requests bypass the page's routes, so none may register."""
    page_host, internal_host = hosts
    internal = f"{internal_host.base_url}/admin?service-worker"
    page = (
        "<p>public page</p><script>navigator.serviceWorker.register('/sw.js')</script>"
        "<script src='/slow.js'></script>"
    )
    page_host.route("GET", "/page", text_answer(page))
    worker = f"self.addEventListener('install', event => event.waitUntil(fetch('{internal}')));"
    page_host.route("GET", "/sw.js", text_answer(worker, content_type="text/javascript"))
    page_host.route("GET", "/slow.js", slow_script)

    text = load(playwright_client, f"{page_host.base_url}/page")

    assert text and "public page" in text, f"the page itself did not load: {text!r}"
    assert internal_host.received == [], "a service worker reached the internal host"


@pytest.mark.requires_browser
@LOADERS
def test_a_websocket_cannot_reach_an_internal_host(playwright_client, hosts, load):
    page_host, internal_host = hosts
    socket_url = f"ws://{INTERNAL_HOST}:{internal_host.port}/socket"
    page = f"<p>public page</p><script>new WebSocket('{socket_url}')</script>"
    page_host.route("GET", "/page", text_answer(page + "<script src='/slow.js'></script>"))
    page_host.route("GET", "/slow.js", slow_script)

    text = load(playwright_client, f"{page_host.base_url}/page")

    assert text and "public page" in text, f"the page itself did not load: {text!r}"
    assert internal_host.received == [], "the page's websocket reached the internal host"
