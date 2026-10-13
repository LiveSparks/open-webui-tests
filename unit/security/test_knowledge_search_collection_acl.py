"""Knowledge-base search must not return chunks of knowledge bases the caller cannot open.

`query_knowledge_bases` hands the vector store `filter={'knowledge_base_id': {'$in': [...]}}`,
the knowledge bases the caller may read. Before commit 1d6d4e6e6 (v0.11.1) eleven bundled
backends took that `filter` argument of `search()` and dropped it, so the store answered with
every neighbour, forbidden knowledge bases included.

integration/security/test_knowledge_search_collection_acl.py runs the search over HTTP on Chroma,
on Qdrant in both storage modes, on Elasticsearch and on Pinecone. What stays here is the audit
over every bundled backend, which also covers the ones added after this fix.

Discriminates: passes on ef67cc3fa, fails with 1d6d4e6e6 reverted (the audit lists the eleven
backends whose `search` ignores `filter`).
"""

from __future__ import annotations

import ast

import pytest

pytestmark = pytest.mark.regression


def filter_parameters_never_read(source: str) -> list[str]:
    unread = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        if "filter" not in {argument.arg for argument in arguments}:
            continue
        loaded = {
            name.id
            for name in ast.walk(node)
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
        }
        if "filter" not in loaded:
            unread.append(node.name)
    return unread


def test_no_vector_backend_ignores_the_filter_it_is_given(open_webui_backend):
    backends = sorted((open_webui_backend / "open_webui/retrieval/vector/dbs").glob("*.py"))
    sources = {path.stem: path.read_text(encoding="utf-8") for path in backends}
    assert any("def search(" in source for source in sources.values()), (
        "no vector backend defines search() any more; retarget this audit"
    )
    unread = {
        f"{name}.{function}"
        for name, source in sources.items()
        for function in filter_parameters_never_read(source)
    }
    assert not unread, f"these vector backend methods drop their `filter` argument: {unread}"
