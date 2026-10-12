"""Refresh a pytest-split durations file from the JUnit reports of a CI run.

python scripts/refresh_durations.py .test_durations reports/*/junit.xml

Every test a report measured gets the mean of that time and its previous one (setup, call and
teardown, so a module's instance boot counts against its first test); tests no report ran keep
their old entry. Shards split on these times, so times measured on the CI runners balance the
shards the CI runners run.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
# pytest-playwright names the browser in a test's id ("[chromium]", "[chromium-on]") on some
# versions only; the durations file keys tests the way the locked version collects them
BROWSER_PARAMETER = re.compile(r"\[chromium-?(.*)\]$")


def node_id(classname: str, name: str) -> str | None:
    """`integration.x.test_y` and `test_z[a]` as `integration/x/test_y.py::test_z[a]`."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        module = Path(*parts[:end]).with_suffix(".py")
        if (ROOT / module).is_file():
            test = "::".join([module.as_posix(), *parts[end:], name])
            return BROWSER_PARAMETER.sub(lambda match: f"[{match[1]}]" if match[1] else "", test)
    return None


def measured(reports: list[Path]) -> dict[str, float]:
    times: dict[str, float] = {}
    for report in reports:
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            if case.find("skipped") is not None:
                continue
            test = node_id(case.get("classname", ""), case.get("name", ""))
            if test:
                times[test] = round(float(case.get("time", 0)), 3)
    return times


def main() -> None:
    target, *reports = (Path(argument) for argument in sys.argv[1:])
    durations = json.loads(target.read_text()) if target.is_file() else {}
    times = measured(reports)
    # the same module runs up to twice as long from one run to the next; averaging with the
    # previous time keeps one slow run from reshuffling every shard
    for test, seconds in times.items():
        previous = durations.get(test)
        durations[test] = round((previous + seconds) / 2, 3) if previous else seconds
    target.write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")
    print(f"{len(times)} measured times from {len(reports)} reports written to {target}")


if __name__ == "__main__":
    main()
