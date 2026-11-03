"""Dependency contract: accelerate stays OUT of this fork.

The fork's no-local-machine ruling strips the whole HF stack (torch,
torchvision, sentence-transformers, accelerate, onnxruntime,
faster-whisper) from the language: the deployment runs external RAG and
LLM providers only, never a local model, and wheels must not drag CUDA
sets. So the upstream surface contract this file used to pin is
unobservable here, and the honest fork contract is the absence.

Discriminates: passes on the fork tree; fails the day an HF pin creeps
back into requirements.txt or a module imports accelerate again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.depcheck

IMPORT_NAME = "accelerate"
DIST_NAME = "accelerate"


def test_accelerate_stays_out_of_the_venv(depcheck):
    # try_load, not load: load() would skip this test on the absence it asserts.
    is_importable = depcheck.try_load(IMPORT_NAME) is not None
    assert (
        not is_importable and depcheck.dist_version(DIST_NAME) is None
    ), "accelerate crept back into the harness venv / requirements"


def test_backend_does_not_import_accelerate(open_webui_backend):
    hits = []
    for path in sorted((open_webui_backend / "open_webui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        if IMPORT_NAME in imports:
            hits.append(str(path.relative_to(open_webui_backend)))
    assert not hits, f"backend imports accelerate again: {hits}"
