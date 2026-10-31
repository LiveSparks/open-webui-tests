"""
PR7 parity module: agent code interpreter.

Asserts that the bridged `execute_code` result translates into the native
`open_webui:code_interpreter` item — the browser's "Analyzed" block — with
streamed code deltas, stdout, stderr, a result value, and base64 image
extraction. The backend translation contract lives in
`backend/open_webui/routers/agent.py` (`_to_code_interpreter_item`) and its
shape contract lives in `sidecar/src/codeInterpreter.ts`.
"""

import json
import logging
import re
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-code-interpreter-parity")


# ---------------------------------------------------------------------------
# Translation mirror (the backend contract, lifted from routers/agent.py)
# ---------------------------------------------------------------------------

_IMAGE_RE = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)")


def to_code_interpreter_item(code: str, result: dict) -> dict:
    images = []
    for text in (result.get("stdout"), result.get("stderr"), result.get("result")):
        if isinstance(text, str):
            for mime, payload in _IMAGE_RE.findall(text):
                images.append({"type": mime.replace("image/", ""), "base64": "".join(payload.split())})
    return {
        "type": "open_webui:code_interpreter",
        "code": code or "",
        "stdout": result.get("stdout") or "",
        "stderr": result.get("stderr") or "",
        "result": result.get("result") or "",
        "images": images,
    }


def code_delta(code: str, delta: str) -> str:
    return (code or "") + delta


# ---------------------------------------------------------------------------
# Parity assertions
# ---------------------------------------------------------------------------

def run_parity() -> bool:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        checks.append((name, ok, detail))
        if not ok:
            logger.error("PARITY FAIL %s %s", name, detail)
        else:
            logger.info("PARITY PASS %s", name)

    # 1. Item shape: a result lands as the native item with all four fields.
    item = to_code_interpreter_item(
        "print('hello')",
        {"stdout": "hello\n", "stderr": "", "result": "'hello'"},
    )
    check("shape.type", item.get("type") == "open_webui:code_interpreter", json.dumps(item.get("type")))
    check(
        "shape.fields",
        item.get("code") == "print('hello')"
        and item.get("stdout") == "hello\n"
        and item.get("stderr") == ""
        and item.get("result") == "'hello'",
        json.dumps({k: item.get(k) for k in ("code", "stdout", "stderr", "result")}),
    )
    check("shape.no_images_on_plain", item.get("images") == [], json.dumps(item.get("images")))

    # 2. Empty result keeps the shape and defaults every field to ''.
    empty = to_code_interpreter_item("", {})
    check(
        "empty.shape",
        empty.get("type") == "open_webui:code_interpreter"
        and empty.get("stdout") == ""
        and empty.get("stderr") == ""
        and empty.get("result") == ""
        and empty.get("images") == [],
    )

    # 3. Image extraction: a base64 data URI in stdout becomes an image.
    png_payload = "iVBORw0KGgoAAAANSUhEUg=="
    image_item = to_code_interpreter_item(
        "import matplotlib.pyplot as plt\nplt.savefig('/tmp/x.png')",
        {"stdout": f"data:image/png;base64,{png_payload}", "stderr": "", "result": ""},
    )
    check("image.found", len(image_item.get("images") or []) == 1, json.dumps(image_item.get("images")))
    if image_item.get("images"):
        img = image_item["images"][0]
        check(
            "image.shape",
            img.get("type") == "png" and img.get("base64") == png_payload,
            json.dumps(img),
        )

    # 4. Image extraction from result (not just stdout).
    image_item_result = to_code_interpreter_item(
        "plt.show()",
        {"stdout": "", "stderr": "", "result": f"data:image/png;base64,{png_payload}"},
    )
    check(
        "image.from_result",
        any(img.get("base64") == png_payload for img in (image_item_result.get("images") or [])),
    )

    # 5. Multiple images across fields are all collected.
    multi = to_code_interpreter_item(
        "",
        {
            "stdout": f"data:image/png;base64,{png_payload}",
            "stderr": f"data:image/jpeg;base64,/9j/4AAQSkZJRg==",
            "result": "",
        },
    )
    check("image.multiple", len(multi.get("images") or []) == 2, json.dumps(multi.get("images")))

    # 6. Code deltas: the streamed code text grows without dropping slices.
    full_code = ""
    for delta in ["import ", "math\n", "print(math.sqrt(4))"]:
        full_code = code_delta(full_code, delta)
    check("delta.grow", full_code == "import math\nprint(math.sqrt(4))", full_code)
    check("delta.empty_start", code_delta("", "x") == "x")

    # 7. Trailing whitespace in the base64 payload is stripped.
    spaced = to_code_interpreter_item("", {"stdout": f"data:image/png;base64, {png_payload} \n", "stderr": "", "result": ""})
    check("image.strip_whitespace", (spaced.get("images") or [{}])[0].get("base64") == png_payload if spaced.get("images") else False)

    passed = all(ok for _, ok, _ in checks)
    logger.info("parity agent_code_interpreter: %d checks, %d passed", len(checks), sum(1 for _, ok, _ in checks if ok))
    return passed


if __name__ == "__main__":
    ok = run_parity()
    sys.exit(0 if ok else 1)
