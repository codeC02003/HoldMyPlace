"""Render the demo by injecting engine-computed data into the template.

    python -m holdmyplace.demo.build [-o demo/index.html]

The template holds no rules and no numbers, only a `__SCENARIO_JSON__`
placeholder. Regenerating after a change to `holdmyplace.domain` updates the
screens, so the demo cannot quietly disagree with the system it illustrates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scenario import build_scenario

PLACEHOLDER = "__SCENARIO_JSON__"
TEMPLATE = Path(__file__).with_name("template.html")
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "demo" / "index.html"


def render() -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise ValueError(f"{TEMPLATE.name} is missing the {PLACEHOLDER} placeholder")

    payload = json.dumps(build_scenario(), default=str)
    # `</script>` inside a JSON string would close the host script element.
    payload = payload.replace("</", "<\\/")
    return template.replace(PLACEHOLDER, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="holdmyplace.demo.build")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    html = render()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(f"wrote {args.out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
