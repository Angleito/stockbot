#!/usr/bin/env python3
"""Generate .stockbot/tools/ catalog from TOOL_DISCOVERY_REGISTRY. Only writer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import (  # noqa: E402
    DOMAIN_DESCRIPTIONS,
    TOOL_DISCOVERY_REGISTRY,
    TOOLS,
    ToolDiscovery,
    _discovery_keywords,
    _tool_function,
)

CATALOG_ROOT = Path(__file__).resolve().parent.parent / ".stockbot" / "tools"

# Discovery primitives never get catalog pages.
EXCLUDED = frozenset({"search_tools", "list_tool_domains", "describe_tool", "browse_tools", "call_tool"})


def _yaml_str(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _tool_params(name: str) -> dict[str, object]:
    for tool in TOOLS:
        fn = _tool_function(tool)
        if fn.get("name") == name:
            params = fn.get("parameters")
            if not isinstance(params, dict):
                raise ValueError(f"tool {name!r} schema has no parameters object")
            return params
    raise ValueError(f"tool {name!r} has no canonical schema in TOOLS")


def _required_args(params: dict[str, object]) -> list[str]:
    required = params.get("required", [])
    return [str(r) for r in required] if isinstance(required, list) else []


def _schema_props(name: str, params: dict[str, object]) -> dict[str, object]:
    props = params.get("properties")
    if not isinstance(props, dict):
        raise ValueError(f"tool {name!r} schema has no properties")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return props


def _prop_detail(spec: object) -> dict[str, str]:
    detail: dict[str, str] = {}
    if isinstance(spec, dict):
        if isinstance(spec.get("type"), str):
            detail["type"] = spec["type"]
        if isinstance(spec.get("description"), str):
            detail["desc"] = spec["description"]
    return detail


def _typed_props(props: dict[str, object]) -> dict[str, dict[str, str]]:
    return {prop: _prop_detail(spec) for prop, spec in sorted(props.items())}


def _schema_args(name: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    params = _tool_params(name)
    return _required_args(params), _typed_props(_schema_props(name, params))


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


def _meta_header_lines(name: str, meta: ToolDiscovery) -> list[str]:
    return [
        f"# {name}\n",
        "\n",
        f"Domain: {meta.domain}\n",
        f"Family: {meta.family}\n",
        f"Intent: {meta.intent}\n",
        f"Output kind: {meta.output_kind}\n",
        f"Source: {meta.source}\n",
        f"Entity scope: {meta.entity_scope}\n",
        f"Time mode: {meta.time_mode}\n",
        "\n",
        f"{meta.summary}\n",
        "\n",
    ]


def _bullets_section(title: str, items: tuple[str, ...]) -> list[str]:
    return [f"## {title}\n", "\n", _bullets(items), "\n"]


def _args_block(title: str, names: list[str], typed: dict[str, dict[str, str]]) -> list[str]:
    body = "".join(_arg_line(a, typed[a]) + "\n" for a in names) or "None\n"
    return [f"## {title}\n", "\n", body]


def tool_markdown(name: str) -> str:
    meta = TOOL_DISCOVERY_REGISTRY[name]
    required, typed = _schema_args(name)
    lines = _meta_header_lines(name, meta)
    for title, items in (
        ("Choose when", meta.choose_when),
        ("Reject when", meta.reject_when),
        ("Conflicts with", meta.conflicts_with),
        ("Related tools", meta.related_tools),
        ("Prerequisites", meta.prerequisites),
    ):
        lines += _bullets_section(title, items)
    lines += _args_block("Required arguments", sorted(required), typed)
    lines.append("\n")
    lines += _args_block("Optional arguments", sorted(a for a in typed if a not in required), typed)
    return "".join(lines)

def _sort_key(name: str) -> tuple[str, str, str]:
    meta = TOOL_DISCOVERY_REGISTRY[name]
    return (meta.domain, meta.family, name)


def _card_keywords(name: str) -> list[str]:
    """Routing keywords derived from existing registry fields (no new schema)."""
    meta = TOOL_DISCOVERY_REGISTRY[name]
    return sorted(_discovery_keywords(
        " ".join((
            name.replace("_", " "),
            meta.intent.replace("_", " "),
            meta.summary,
            " ".join(meta.choose_when),
        ))
    ))


def _domain_lines(names: list[str]) -> list[str]:
    lines: list[str] = []
    for domain in sorted({TOOL_DISCOVERY_REGISTRY[n].domain for n in names}):
        lines.append(f"  {domain}:\n")
        lines.append(f"    description: {_yaml_str(DOMAIN_DESCRIPTIONS[domain])}\n")
        fams = sorted({TOOL_DISCOVERY_REGISTRY[n].family for n in names if TOOL_DISCOVERY_REGISTRY[n].domain == domain})
        if fams:
            lines.append("    families:\n")
        for fam in fams:
            lines.append(f"      - {fam}:\n")
            lines.append(f"        path: {_yaml_str(f'/{domain}/{fam}')}\n")
    return lines


def _tool_card_lines(name: str) -> list[str]:
    meta = TOOL_DISCOVERY_REGISTRY[name]
    return [
        f"  - name: {name}\n",
        f"    domain: {meta.domain}\n",
        f"    family: {meta.family}\n",
        f"    path: {_yaml_str(f'/{meta.domain}/{meta.family}/{name}')}\n",
        f"    intent: {meta.intent}\n",
        f"    output_kind: {meta.output_kind}\n",
        f"    source: {meta.source}\n",
        f"    entity_scope: {meta.entity_scope}\n",
        f"    time_mode: {meta.time_mode}\n",
        f"    summary: {_yaml_str(meta.summary)}\n",
        "    version: 2\n",
        f"    keywords: [{', '.join(_card_keywords(name))}]\n",
    ]


def index_yaml(names: list[str]) -> str:
    lines = ["version: 2\n", "domains:\n"]
    lines += _domain_lines(names)
    lines.append("tools:\n")
    for name in sorted(names, key=_sort_key):
        lines += _tool_card_lines(name)
    return "".join(lines)


def validate_registry(names: list[str]) -> None:
    for name in names:
        meta = TOOL_DISCOVERY_REGISTRY[name]
        if meta.domain not in DOMAIN_DESCRIPTIONS:
            raise ValueError(f"tool {name!r} has undescribed domain {meta.domain!r}")
        for ref in (*meta.related_tools, *meta.prerequisites, *meta.conflicts_with):
            if ref not in TOOL_DISCOVERY_REGISTRY:
                raise ValueError(f"tool {name!r} references unknown tool {ref!r}")
        _schema_args(name)  # missing schema raises


def write_catalog(names: list[str], root: Path = CATALOG_ROOT) -> int:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.yaml").write_text(index_yaml(names))
    for name in names:
        meta = TOOL_DISCOVERY_REGISTRY[name]
        path = root / meta.domain / meta.family / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tool_markdown(name))
    return len(names)


def catalog_names() -> list[str]:
    return sorted(n for n in TOOL_DISCOVERY_REGISTRY if n not in EXCLUDED)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate the registry without writing the catalog")
    return parser.parse_args(argv)


def report_and_write(names: list[str], check_only: bool) -> int:
    validate_registry(names)
    if check_only:
        print(f"registry ok: {len(names)} tools")
        return 0
    count = write_catalog(names)
    print(f"wrote {count} tools to {CATALOG_ROOT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return report_and_write(catalog_names(), args.check)


if __name__ == "__main__":
    sys.exit(main())
