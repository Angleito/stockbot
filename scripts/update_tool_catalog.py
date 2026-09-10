#!/usr/bin/env python3
"""Generate .stockbot/tools/ catalog from TOOL_DISCOVERY_REGISTRY. Only writer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import DOMAIN_DESCRIPTIONS, TOOLS, TOOL_DISCOVERY_REGISTRY, _tool_function  # noqa: E402

CATALOG_ROOT = Path(__file__).resolve().parent.parent / ".stockbot" / "tools"

# Discovery primitives never get catalog pages.
EXCLUDED = frozenset({"search_tools", "list_tool_domains", "describe_tool"})


def _yaml_str(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _schema_args(name: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    for tool in TOOLS:
        fn = _tool_function(tool)
        if fn.get("name") == name:
            params = fn.get("parameters")
            if not isinstance(params, dict):
                raise ValueError(f"tool {name!r} schema has no parameters object")
            props = params.get("properties")
            if not isinstance(props, dict):
                raise ValueError(f"tool {name!r} schema has no properties")
            required = [str(r) for r in params.get("required", [])]
            typed: dict[str, dict[str, str]] = {}
            for prop, spec in sorted(props.items()):
                detail: dict[str, str] = {}
                if isinstance(spec, dict):
                    if isinstance(spec.get("type"), str):
                        detail["type"] = str(spec["type"])
                    if isinstance(spec.get("description"), str):
                        detail["desc"] = str(spec["description"])
                typed[str(prop)] = detail
            return required, typed
    raise ValueError(f"tool {name!r} has no canonical schema in TOOLS")


def _arg_line(arg: str, detail: dict[str, str]) -> str:
    label = f"`{arg}`"
    if detail.get("type"):
        label += f" ({detail['type']})"
    if detail.get("desc"):
        label += f": {detail['desc']}"
    return f"- {label}"


def _bullets(items: tuple[str, ...]) -> str:
    if not items:
        return "None\n"
    return "".join(f"- {item}\n" for item in items)


def tool_markdown(name: str) -> str:
    meta = TOOL_DISCOVERY_REGISTRY[name]
    required, typed = _schema_args(name)
    optional = sorted(a for a in typed if a not in required)
    lines = [
        f"# {name}\n",
        "\n",
        f"Domain: {meta.domain}\n",
        "\n",
        f"{meta.summary}\n",
        "\n",
        "## Use when\n",
        "\n",
        _bullets(meta.use_when),
        "\n",
        "## Avoid when\n",
        "\n",
        _bullets(meta.avoid_when),
        "\n",
        "## Related tools\n",
        "\n",
        _bullets(meta.related_tools),
        "\n",
        "## Prerequisites\n",
        "\n",
        _bullets(meta.prerequisites),
        "\n",
        "## Required arguments\n",
        "\n",
        "".join(_arg_line(a, typed[a]) + "\n" for a in sorted(required)) or "None\n",
        "\n",
        "## Optional arguments\n",
        "\n",
        "".join(_arg_line(a, typed[a]) + "\n" for a in optional) or "None\n",
    ]
    return "".join(lines)

def _sort_key(name: str) -> tuple[str, str]:
    meta = TOOL_DISCOVERY_REGISTRY[name]
    return (meta.domain, name)


def index_yaml(names: list[str]) -> str:
    domains = sorted({TOOL_DISCOVERY_REGISTRY[n].domain for n in names})
    lines = ["version: 1\n", "domains:\n"]
    for domain in domains:
        lines.append(f"  {domain}:\n")
        lines.append(f"    description: {_yaml_str(DOMAIN_DESCRIPTIONS[domain])}\n")
    lines.append("tools:\n")
    for name in sorted(names, key=_sort_key):
        meta = TOOL_DISCOVERY_REGISTRY[name]
        lines.append(f"  - name: {name}\n")
        lines.append(f"    domain: {meta.domain}\n")
        lines.append(f"    summary: {_yaml_str(meta.summary)}\n")
    return "".join(lines)


def main() -> int:
    names = sorted(n for n in TOOL_DISCOVERY_REGISTRY if n not in EXCLUDED)
    for name in names:
        meta = TOOL_DISCOVERY_REGISTRY[name]
        if meta.domain not in DOMAIN_DESCRIPTIONS:
            raise ValueError(f"tool {name!r} has undescribed domain {meta.domain!r}")
        for ref in (*meta.related_tools, *meta.prerequisites):
            if ref not in TOOL_DISCOVERY_REGISTRY:
                raise ValueError(f"tool {name!r} references unknown tool {ref!r}")
        _schema_args(name)  # missing schema raises
    CATALOG_ROOT.mkdir(parents=True, exist_ok=True)
    (CATALOG_ROOT / "index.yaml").write_text(index_yaml(names))
    for name in names:
        domain = TOOL_DISCOVERY_REGISTRY[name].domain
        path = CATALOG_ROOT / domain / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tool_markdown(name))
    print(f"wrote {len(names)} tools to {CATALOG_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
