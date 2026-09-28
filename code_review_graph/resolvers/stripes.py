"""Post-build resolution of Stripes ActionBeans: endpoints, handlers, forwards.

Stripes binds actions to URLs with a class-level ``@UrlBinding`` and picks a
handler per request with ``@HandlesEvent`` / ``@DefaultHandler`` — all inside
ordinary Java source the per-file parser reads without web semantics. This
resolver owns the derived web graph:

  Endpoint     one node per ``@UrlBinding`` route (``extra.route`` carries the
               URL and ``extra.handler_qualified`` the bean class, so the JSP
               resolver and the query layer can bind pages to it)
  HANDLES      handler method -> its bean's Endpoint node
  FORWARDS_TO  handler method -> page File / bean Class / Endpoint node for
               every ``ForwardResolution`` / ``RedirectResolution`` built in
               that method

Targets bind only to real graph nodes: a forward path must exist under the
web root, a ``*.class`` resolution must match one Class node, a resolution
URL must match a known route — anything else is counted and skipped, never
guessed. Runs with the framework-level conventions of
:mod:`code_review_graph.repo_config` (the shared frontend table drives the
web root and source root for every frontend-facing resolver).

Idempotent: it deletes its own Endpoint nodes and edges first and re-derives
everything from live graph state, so rebuilds converge and deletions never
leave stale rows behind.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..parser import EdgeInfo, NodeInfo
from ..repo_config import JspResolverConfig, load_jsp_resolver_config
from .jsp import (
    _class_index,
    _endpoint_map,
    _java_sources,
    _Lines,
    _normalize_url,
)

if TYPE_CHECKING:
    from ..graph import GraphStore

logger = logging.getLogger(__name__)

URL_BINDING = re.compile(r'@UrlBinding\s*\(\s*"([^"]+)"\s*\)')
# The word boundary belongs to DefaultHandler only: after HandlesEvent's
# closing paren there is no boundary between ")" and the newline.
EVENT_ANNO = re.compile(r'@(?:HandlesEvent\s*\(\s*"([^"]*)"\s*\)|DefaultHandler\b)')
SIGNATURE = re.compile(
    r"\b(?:public|protected)\s+(?:static\s+|final\s+|synchronized\s+)*"
    r"[\w<>\[\],.\s?]+?\s(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+?)?\{"
)
FORWARD_PATH = re.compile(r'new\s+ForwardResolution\s*\(\s*"([^"]+)"')
FORWARD_CLASS = re.compile(r"new\s+ForwardResolution\s*\(\s*(\w+)\.class")
REDIRECT_PATH = re.compile(r'new\s+RedirectResolution\s*\(\s*"([^"]+)"')
REDIRECT_CLASS = re.compile(r"new\s+RedirectResolution\s*\(\s*(\w+)\.class")

_MARKER = "UrlBinding"

class _MethodSpan(NamedTuple):
    name: str
    line: int
    annotations: str
    body: str
    body_offset: int


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _matching_brace(text: str, open_index: int) -> int:
    """Index of the ``}`` closing *open_index*, skipping literals and comments."""
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ('"', "'"):
            i += 1
            while i < n and text[i] != ch:
                i += 2 if text[i] == "\\" else 1
        elif ch == "/" and text[i + 1 : i + 2] == "/":
            newline = text.find("\n", i)
            i = newline if newline != -1 else n
            continue
        elif ch == "/" and text[i + 1 : i + 2] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _methods(body: str, body_offset: int, lines: _Lines) -> list[_MethodSpan]:
    """Public/protected methods of one class body, each with its annotation gap.

    The gap is the text between the previous member's closing brace and this
    signature — exactly where ``@HandlesEvent`` / ``@DefaultHandler`` sit.
    """
    found: list[_MethodSpan] = []
    pos = 0
    prev_end = 0
    while match := SIGNATURE.search(body, pos):
        open_brace = match.end() - 1
        close = _matching_brace(body, open_brace)
        found.append(_MethodSpan(
            name=match.group(1),
            line=lines.of(body_offset + match.start()),
            annotations=body[prev_end:match.start()],
            body=body[open_brace:close + 1],
            body_offset=body_offset + open_brace,
        ))
        pos = prev_end = close + 1
    return found


def _class_body(text: str, klass: str) -> tuple[str, int] | None:
    """Body (braces included) of *klass* and its offset in *text*, or None."""
    match = re.search(rf"\bclass\s+{re.escape(klass)}\b[^{{]*\{{", text)
    if match is None:
        return None
    open_brace = match.end() - 1
    close = _matching_brace(text, open_brace)
    return text[open_brace:close + 1], open_brace


def _scan_beans(
    repo_root: Path, config: JspResolverConfig,
) -> list[tuple[str, str, str]]:
    """``(file, class, route)`` for every ``@UrlBinding`` bean under the source root."""
    source_root = repo_root / config.source_root
    if not source_root.is_dir():
        return []
    beans: list[tuple[str, str, str]] = []
    for path in _java_sources(repo_root, source_root):
        text = _read(path)
        if "@UrlBinding" not in text:
            continue
        for match in URL_BINDING.finditer(text):
            klass_match = re.search(r"\bclass\s+(\w+)", text[match.start():])
            if klass_match is not None:
                beans.append((path.as_posix(), klass_match.group(1), match.group(1)))
    return beans


def _class_qn_in_file(
    classes: dict[str, list[tuple[str, str]]], klass: str, path: str,
) -> str | None:
    """Class-node qualified name for *klass* declared in *path*, if unique."""
    matches = [qn for qn, file in classes.get(klass, ()) if file == path]
    return matches[0] if len(matches) == 1 else None


def _file_index(conn) -> set[str]:
    rows = conn.execute("SELECT qualified_name FROM nodes WHERE kind = 'File'")
    return {row["qualified_name"] for row in rows}


def _function_index(conn) -> set[str]:
    rows = conn.execute(
        "SELECT qualified_name FROM nodes WHERE kind = 'Function' AND language = 'java'"
    )
    return {row["qualified_name"] for row in rows}


def _path_target(
    raw: str, config: JspResolverConfig, files: set[str], web_root: Path,
) -> str | None:
    """File node for a web-root-relative resolution path, if it exists."""
    href = raw.split("?", 1)[0].split("#", 1)[0].strip()
    if not href.startswith("/"):
        return None
    candidate = (web_root / href.lstrip("/")).as_posix()
    return candidate if candidate in files else None


def _route_target(
    raw: str, config: JspResolverConfig, endpoints: dict[str, str],
) -> tuple[str, str] | None:
    """``(endpoint QN, normalized route)`` for a resolution URL, if bound."""
    route = _normalize_url(raw, config.dead_url_suffixes)
    if not route:
        return None
    target = endpoints.get(route)
    return (target, route) if target is not None else None


def _class_target(
    simple: str, classes: dict[str, list[tuple[str, str]]],
) -> str | None:
    """Class node for a ``Something.class`` literal, if unambiguous."""
    entries = classes.get(simple, ())
    return entries[0][0] if len(entries) == 1 else None


def _resolution_targets(
    body: str,
    body_offset: int,
    lines: _Lines,
    config: JspResolverConfig,
    files: set[str],
    web_root: Path,
    classes: dict[str, list[tuple[str, str]]],
    endpoints: dict[str, str],
) -> tuple[list[tuple[str, int, dict]], int]:
    """Bind every resolution built in *body* to a graph node.

    A forward names a page (file first, then a routed URL); a redirect is a
    URL (route first, then a file path); either may carry a ``.class``
    literal naming the next bean. Returns ``([(target, line, extra)], misses)``.
    """
    targets: list[tuple[str, int, dict]] = []
    misses = 0

    def line_of(match: re.Match[str]) -> int:
        return lines.of(body_offset + match.start())

    for match in FORWARD_CLASS.finditer(body):
        target = _class_target(match.group(1), classes)
        if target is None:
            misses += 1
            continue
        targets.append((target, line_of(match), {"resolution": "forward"}))
    for match in FORWARD_PATH.finditer(body):
        raw = match.group(1)
        file_qn = _path_target(raw, config, files, web_root)
        if file_qn is not None:
            targets.append((file_qn, line_of(match), {"resolution": "forward"}))
            continue
        routed = _route_target(raw, config, endpoints)
        if routed is not None:
            targets.append((
                routed[0], line_of(match),
                {"resolution": "endpoint", "route": routed[1], "url": raw},
            ))
            continue
        misses += 1
    for match in REDIRECT_CLASS.finditer(body):
        target = _class_target(match.group(1), classes)
        if target is None:
            misses += 1
            continue
        targets.append((target, line_of(match), {"resolution": "redirect"}))
    for match in REDIRECT_PATH.finditer(body):
        raw = match.group(1)
        routed = _route_target(raw, config, endpoints)
        if routed is not None:
            targets.append((
                routed[0], line_of(match),
                {"resolution": "endpoint", "route": routed[1], "url": raw},
            ))
            continue
        file_qn = _path_target(raw, config, files, web_root)
        if file_qn is not None:
            targets.append((file_qn, line_of(match), {"resolution": "redirect"}))
            continue
        misses += 1
    return targets, misses


def resolve_stripes_actions(store: GraphStore, repo_root: Path) -> dict[str, int]:
    """Rebuild Stripes Endpoint/HANDLES/FORWARDS_TO graph state in one pass.

    Every edge is re-derived from the Java sources currently on disk plus the
    graph's own File/Class/Function nodes; a repository without Stripes beans
    is a clean no-op that still clears this resolver's stale state. The write
    is diff-based: unchanged endpoints and edges are only re-upserted (stable
    row ids, no journal churn), and only genuinely stale rows are deleted.
    """
    with store.transaction():
        return _resolve(store, repo_root)


def _resolve(store: GraphStore, repo_root: Path) -> dict[str, int]:
    repo_root = Path(repo_root).resolve()
    config = load_jsp_resolver_config(repo_root) or JspResolverConfig()

    conn = store._conn  # intentional: bounded post-build maintenance pass
    beans = _scan_beans(repo_root, config)

    classes = _class_index(conn)
    functions = _function_index(conn)
    files = _file_index(conn)
    endpoints = _endpoint_map(conn, config.dead_url_suffixes)
    web_root = (repo_root / config.web_root).resolve()

    wanted_endpoints: dict[str, NodeInfo] = {}
    wanted_handles: dict[tuple[str, str, str, int], dict] = {}
    wanted_forwards: dict[tuple[str, str, str, int], dict] = {}
    unresolved = 0

    for path, klass, raw_route in beans:
        route = _normalize_url(raw_route, config.dead_url_suffixes)
        if not route:
            continue
        text = _read(Path(path))
        bean_qn = _class_qn_in_file(classes, klass, path)
        endpoint_qn, node = _endpoint_node(path, klass, route, bean_qn)
        wanted_endpoints[endpoint_qn] = node
        # Existing parser-owned endpoints keep route priority for redirects;
        # handlers always bind to this resolver's own node.
        endpoints.setdefault(route, endpoint_qn)
        read = _class_body(text, klass)
        if read is None:
            continue
        body, offset = read
        lines = _Lines(text)
        for method in _methods(body, offset, lines):
            event = EVENT_ANNO.search(method.annotations)
            if event is None:
                continue
            method_qn = f"{path}::{klass}.{method.name}"
            if method_qn not in functions:
                continue
            wanted_handles[(method_qn, endpoint_qn, path, method.line)] = {
                "annotation": _MARKER,
                "event": event.group(1) or "default",
            }
            targets, misses = _resolution_targets(
                method.body, method.body_offset, lines, config,
                files, web_root, classes, endpoints,
            )
            unresolved += misses
            for target, line, extra in targets:
                wanted_forwards[(method_qn, target, path, line)] = extra

    handles, forwards = _apply(
        store, conn, wanted_endpoints, wanted_handles, wanted_forwards,
    )

    handles, forwards = _apply(
        store, conn, wanted_endpoints, wanted_handles, wanted_forwards,
    )
    store._invalidate_cache()
    result = {
        "beans": len(beans),
        "endpoints": len(wanted_endpoints),
        "handles": handles,
        "forwards": forwards,
        "unresolved_resolutions": unresolved,
    }
    logger.info("Stripes action resolution: %s", result)
    return result


def _endpoint_node(
    path: str, klass: str, route: str, class_qn: str | None,
) -> tuple[str, NodeInfo]:
    """The one Endpoint node for a bean's ``@UrlBinding`` route, and its QN."""
    name = f"{klass}@{_MARKER}[0:0] ANY {route}"
    return f"{path}::{klass}.{name}", NodeInfo(
        kind="Endpoint",
        name=name,
        file_path=path,
        line_start=0,
        line_end=0,
        language="java",
        parent_name=klass,
        extra={
            "annotation": _MARKER,
            "handler": klass,
            "handler_qualified": class_qn or "",
            "http_method": "ANY",
            "route": route,
        },
    )


def _existing_edge_keys(
    conn, kind: str, marker_only: bool,
) -> set[tuple[str, str, str, int]]:
    sql = "SELECT source_qualified, target_qualified, file_path, line FROM edges WHERE kind = ?"
    params: list[str] = [kind]
    if marker_only:
        sql += " AND json_extract(extra, '$.annotation') = ?"
        params.append(_MARKER)
    return {
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(sql, params)
    }


def _apply(
    store: GraphStore,
    conn,
    wanted_endpoints: dict[str, NodeInfo],
    wanted_handles: dict[tuple[str, str, str, int], dict],
    wanted_forwards: dict[tuple[str, str, str, int], dict],
) -> tuple[int, int]:
    """Delete stale rows, then upsert the wanted state (id-stable)."""
    existing_endpoints = {
        row[0] for row in conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind = 'Endpoint' "
            "AND json_extract(extra, '$.annotation') = ?", (_MARKER,),
        )
    }
    stale_endpoints = sorted(existing_endpoints - set(wanted_endpoints))
    if stale_endpoints:
        marks = ", ".join("?" for _ in stale_endpoints)
        conn.execute(
            f"DELETE FROM nodes WHERE qualified_name IN ({marks})", stale_endpoints,  # nosec B608
        )

    for kind, wanted in (
        ("HANDLES", wanted_handles),
        ("FORWARDS_TO", wanted_forwards),
    ):
        marker_only = kind == "HANDLES"
        stale = _existing_edge_keys(conn, kind, marker_only) - set(wanted)
        for source, target, file_path, line in sorted(stale):
            conn.execute(
                f"DELETE FROM edges WHERE kind = '{kind}' AND source_qualified = ? "  # nosec B608
                "AND target_qualified = ? AND file_path = ? AND line = ?",
                (source, target, file_path, line),
            )

    for node in wanted_endpoints.values():
        store.upsert_node(node)
    for (source, target, file_path, line), extra in wanted_handles.items():
        store.upsert_edge(EdgeInfo(
            kind="HANDLES",
            source=source,
            target=target,
            file_path=file_path,
            line=line,
            extra=extra,
        ))
    for (source, target, file_path, line), extra in wanted_forwards.items():
        store.upsert_edge(EdgeInfo(
            kind="FORWARDS_TO",
            source=source,
            target=target,
            file_path=file_path,
            line=line,
            extra=extra,
        ))
    return len(wanted_handles), len(wanted_forwards)
