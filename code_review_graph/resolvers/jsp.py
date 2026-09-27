"""Post-build resolution of JSP/HTML <-> Java and frontend asset links.

The parser indexes ``.jsp``/``.jspf``/``.tag`` files as plain File nodes
(no symbols) and ``.html`` files since the fork; this resolver owns the
edges between them and the rest of the graph. It creates no nodes: every
discovery starts from the graph's own File/Class/Endpoint nodes, so an
older database without jsp File nodes is a clean no-op.

  RENDERS    jsp -> Endpoint/Class/raw (an explicit bean-binding attribute)
  REQUESTS   jsp/js -> Endpoint/Class/raw (an href/action/url/ajax/fetch
                            call to a route-annotated endpoint)
  INCLUDES   jsp -> jsp (``<%@ include %>`` / ``<jsp:include>``)
  REFERENCES jsp/html -> File (script src, stylesheet link, page links)

REQUESTS/RENDERS targets bind to real graph nodes in priority order:
Endpoint nodes (Spring request mappings — handler-method visibility comes
from the Endpoint's existing HANDLES edge), then Java Class nodes by
repository-suffix FQN match, then the raw dotted FQN flagged
``extra.unresolved = true``. The raw FQN and matched route always stay in
the edge ``extra`` so no source data is lost by a failed binding.

Runs with framework-level defaults (see :mod:`code_review_graph.repo_config`);
``[resolvers.jsp]`` overrides them and ``enabled = false`` switches it off.

Idempotent: it deletes its own edge kinds first and re-derives everything
from live graph state, so a full rebuild and an incremental update converge
to the same graph and deletions never leave stale edges behind. REFERENCES
deletion is scoped to jsp/html File sources because other producers (Blade,
HCL) own REFERENCES edges of their own.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..repo_config import JspResolverConfig, load_jsp_resolver_config

if TYPE_CHECKING:
    from ..graph import GraphStore

logger = logging.getLogger(__name__)

JSP_SUFFIXES = (".jsp", ".jspf", ".tag")
PAGE_SUFFIXES = (*JSP_SUFFIXES, ".html", ".htm")

# Vendored, generated or documentation assets: their links say nothing about
# an application's own routing, regardless of which framework generated them.
# Common third-party library and tooling directory names, not tied to any
# one organisation's conventions.
_EXCLUDED_PARTS = (
    "/node_modules/", "/.git/", "/bower_components/", "/vendor/",
    "/jquery", "/bootstrap", "/datatables/", "/ckeditor/",
)
_EXCLUDED_SUFFIXES = (".min.js", ".min.css")

CLASS_DECL = re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)", re.M)
PACKAGE_DECL = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)

HREF_URL = re.compile(r'\b(?:href|action|url)\s*=\s*"([^"]+)"')
AJAX_URL = re.compile(r"""\burl\s*:\s*['"]([^'"]+)['"]""")
FETCH_URL = re.compile(r"""\bfetch\s*\(\s*['"]([^'"]+)['"]""")
INCLUDE_DIRECTIVE = re.compile(r'<%@\s*include\s+file\s*=\s*"([^"]+)"')
INCLUDE_TAG = re.compile(r'<\s*jsp:include\b[^>]*\bpage\s*=\s*"([^"]+)"', re.I | re.S)

SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I | re.S)
LINK_TAG = re.compile(r"<link\b[^>]*>", re.I | re.S)
ANCHOR_HREF = re.compile(r'<a\b[^>]*\bhref\s*=\s*"([^"]+)"', re.I | re.S)
FORM_ACTION = re.compile(r'<form\b[^>]*\baction\s*=\s*"([^"]+)"', re.I | re.S)
TAG_ATTRIBUTE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# Href forms that can never name a repository file: skip them before the
# unresolvable count so it measures real misses, not external links.
_NON_FILE_PREFIXES = (
    "http://", "https://", "//", "data:", "#", "javascript:", "mailto:",
)

_STATS_ZERO = {
    "files_indexed": 0,
    "endpoints": 0,
    "bindings": 0,
    "renders": 0,
    "requests": 0,
    "includes": 0,
    "references": 0,
    "unresolved_references": 0,
    "unresolved_targets": 0,
}

# One extracted edge before dedupe: (kind, source QN, target, line, extra).
_Link = tuple[str, str, str, int, dict]


def _excluded(path_text: str) -> bool:
    text = path_text.replace("\\", "/")
    return any(part in text for part in _EXCLUDED_PARTS) or text.endswith(_EXCLUDED_SUFFIXES)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _route_annotation_pattern(route_annotations: tuple[str, ...]) -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in route_annotations)
    return re.compile(rf'@(?:{names})\s*\(\s*(?:value\s*=\s*)?"([^"]+)"')


def _bean_attribute_pattern(config: JspResolverConfig) -> re.Pattern[str]:
    return re.compile(
        rf'{re.escape(config.bean_attribute)}\s*=\s*"({re.escape(config.bean_package_prefix)}[^"]+)"'
    )


def _normalize_url(raw: str, dead_url_suffixes: tuple[str, ...]) -> str:
    """Reduce a link to the key used in the binding map.

    Drops the query string, dead-routing suffix and EL fragments, so
    ``./editvendor.xhtml?save-settings`` and ``/editvendor.xhtml`` resolve to
    the same binding.
    """
    url = raw.strip()
    if not url or url.startswith(("http://", "https://", "//", "#", "javascript:", "mailto:")):
        return ""
    url = re.sub(r"^\$\{[^}]*\}", "", url)
    if "${" in url or "<%" in url:
        return ""
    url = url.split("?", 1)[0].split("#", 1)[0]
    if url.endswith(dead_url_suffixes):
        return ""
    url = url.lstrip(".")
    if not url.startswith("/"):
        url = "/" + url
    return url if len(url) > 1 else ""


def _binding_map(repo_root: Path, config: JspResolverConfig) -> dict[str, str]:
    """URL path -> fully qualified Java class, from route-annotated classes."""
    route_pattern = _route_annotation_pattern(config.route_annotations)
    bindings: dict[str, str] = {}
    source_root = repo_root / config.source_root
    if not source_root.is_dir():
        return bindings
    for java in source_root.rglob("*.java"):
        text = _read(java)
        if not any(f"@{name}" in text for name in config.route_annotations):
            continue
        package = PACKAGE_DECL.search(text)
        klass = CLASS_DECL.search(text)
        if not package or not klass:
            continue
        fqn = f"{package.group(1)}.{klass.group(1)}"
        for match in route_pattern.finditer(text):
            key = _normalize_url(match.group(1), config.dead_url_suffixes)
            if key:
                bindings.setdefault(key, fqn)
    return bindings


def _graph_pages(conn, language: str) -> list[str]:
    """File-node qualified names (absolute paths) for one page language."""
    rows = conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE kind = 'File' AND language = ? ORDER BY qualified_name",
        (language,),
    ).fetchall()
    return [row["qualified_name"] for row in rows if not _excluded(row["qualified_name"])]


def _graph_scripts(conn) -> list[str]:
    rows = conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE kind = 'File' AND language = 'javascript' ORDER BY qualified_name"
    ).fetchall()
    return [row["qualified_name"] for row in rows if not _excluded(row["qualified_name"])]


def _class_index(conn) -> dict[str, list[tuple[str, str]]]:
    """Java class simple name -> [(qualified_name, file_path)]."""
    index: dict[str, list[tuple[str, str]]] = {}
    rows = conn.execute(
        "SELECT qualified_name, name, file_path FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall()
    for row in rows:
        index.setdefault(row["name"], []).append((row["qualified_name"], row["file_path"]))
    return index


def _endpoint_map(conn, dead_url_suffixes: tuple[str, ...]) -> dict[str, str]:
    """Normalized route -> Endpoint qualified name.

    The parser emits one Endpoint node per (annotation, path, http_method)
    combination, so several can share a route. A page link rarely declares
    its HTTP verb, so ties prefer the GET endpoint, then the smallest
    qualified name — deterministic without pretending to know the verb.
    """
    routes: dict[str, list[tuple[str, str]]] = {}
    rows = conn.execute(
        "SELECT qualified_name, extra FROM nodes WHERE kind = 'Endpoint'"
    ).fetchall()
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(extra, dict):
            continue
        route = _normalize_url(str(extra.get("route", "")), dead_url_suffixes)
        if not route:
            continue
        routes.setdefault(route, []).append(
            (str(extra.get("http_method", "")), row["qualified_name"])
        )
    return {
        route: _preferred_endpoint(candidates)
        for route, candidates in routes.items()
    }


def _preferred_endpoint(candidates: list[tuple[str, str]]) -> str:
    ordered = sorted(candidates)
    for method, qn in ordered:
        if method == "GET":
            return qn
    return ordered[0][1]


def _resolve_class(fqn: str, classes: dict[str, list[tuple[str, str]]]) -> str | None:
    """Bind a dotted FQN to a Class node by repository path suffix.

    A unique file whose path ends with the FQN's package path wins; a lone
    simple-name match is accepted as a fallback; anything ambiguous stays
    unresolved rather than guessing.
    """
    simple = fqn.rsplit(".", 1)[-1]
    entries = classes.get(simple, ())
    if not entries:
        return None
    package_suffix = "/" + fqn.replace(".", "/")
    path_matches = [
        qn for qn, file_path in entries
        if file_path[:-len(".java")].endswith(package_suffix)
    ]
    if len(path_matches) == 1:
        return path_matches[0]
    if not path_matches and len(entries) == 1:
        return entries[0][0]
    return None


def _candidate_hrefs(
    source_qn: str, raw: str, web_root: Path, context_paths: tuple[str, ...],
) -> list[str]:
    """Absolute-path candidates for one href, in probing priority order."""
    href = raw.split("?", 1)[0].split("#", 1)[0].strip()
    if not href or href.startswith(_NON_FILE_PREFIXES):
        return []
    if "${" in href or "<%" in href:
        return []
    candidates: list[str] = []
    if href.startswith("/"):
        rel = href.lstrip("/")
        candidates.append((web_root / rel).as_posix())
        for context in context_paths:
            prefix = context.strip("/")
            if not prefix or (rel != prefix and not rel.startswith(prefix + "/")):
                continue
            stripped = rel[len(prefix):].lstrip("/")
            if stripped:
                candidates.append((web_root / stripped).as_posix())
    else:
        base_dir = posixpath.dirname(source_qn)
        candidates.append(posixpath.normpath(posixpath.join(base_dir, href)))
        candidates.append((web_root / href).as_posix())
    return [posixpath.normpath(candidate) for candidate in candidates]


def _resolve_reference(
    source_qn: str, raw: str, web_root: Path,
    context_paths: tuple[str, ...], file_index: set[str],
) -> tuple[str | None, bool]:
    """Resolve one href against the graph's File nodes.

    Returns ``(resolved_qualified_name, countable)``. ``countable`` is False
    for forms that can never name a repository file (external URLs, EL
    expressions), so the unresolvable-reference stat measures real misses.
    """
    candidates = _candidate_hrefs(source_qn, raw, web_root, context_paths)
    if not candidates:
        return None, False
    for candidate in candidates:
        if candidate in file_index:
            return candidate, True
    return None, True


def _tag_attributes(tag: str) -> dict[str, str]:
    return dict(TAG_ATTRIBUTE.findall(tag))


def _dedupe(links: list[_Link], by_asset: bool = False) -> list[_Link]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[_Link] = []
    for link in links:
        kind, source, target = link[0], link[1], link[2]
        key = (source, target, link[4]["asset"]) if by_asset else (source, target, kind)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def resolve_jsp_links(store: GraphStore, repo_root: Path) -> dict[str, int]:
    """Rebuild RENDERS/REQUESTS/INCLUDES/REFERENCES edges from graph state.

    Every edge is re-derived from the nodes currently in the graph: an older
    database without jsp File nodes, or a repository without any pages, is a
    clean no-op that still clears this resolver's stale edges. The delete and
    the re-insert commit together, so a failure keeps the previous edges.
    """
    with store.transaction():
        return _resolve_jsp_links(store, repo_root)


def _resolve_jsp_links(store: GraphStore, repo_root: Path) -> dict[str, int]:
    repo_root = Path(repo_root).resolve()
    config = load_jsp_resolver_config(repo_root)
    if config is not None and not config.enabled:
        return dict(_STATS_ZERO)
    if config is None:
        config = JspResolverConfig()

    conn = store._conn  # intentional: bounded post-build maintenance pass

    # Idempotent rebuild: clear this resolver's own rows before recomputing
    # them. RENDERS/REQUESTS/INCLUDES have no other producer; REFERENCES
    # does (Blade, HCL), so its deletion is scoped to page-file sources —
    # including edges whose page File node is already gone, which would
    # otherwise survive every rebuild as stale rows.
    conn.execute("DELETE FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES')")
    conn.execute(
        "DELETE FROM edges WHERE kind = 'REFERENCES' AND source_qualified IN ("
        "SELECT qualified_name FROM nodes "
        "WHERE kind = 'File' AND language IN ('jsp', 'html'))"
    )
    stale_page_refs = " OR ".join(
        f"file_path LIKE '%{suffix}'" for suffix in PAGE_SUFFIXES
    )
    conn.execute(
        "DELETE FROM edges WHERE kind = 'REFERENCES' "
        "AND source_qualified NOT IN (SELECT qualified_name FROM nodes) "
        f"AND ({stale_page_refs})"
    )

    jsp_pages = _graph_pages(conn, "jsp")
    html_pages = _graph_pages(conn, "html")
    scripts = _graph_scripts(conn)
    if not jsp_pages and not html_pages and not scripts:
        store._invalidate_cache()
        return dict(_STATS_ZERO)

    file_index = {
        row["qualified_name"]
        for row in conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind = 'File'"
        ).fetchall()
    }
    classes = _class_index(conn)
    endpoints = _endpoint_map(conn, config.dead_url_suffixes)
    bindings = _binding_map(repo_root, config)
    bean_pattern = _bean_attribute_pattern(config)
    web_root = (repo_root / config.web_root).resolve()

    renders: list[_Link] = []
    requests: list[_Link] = []
    includes: list[_Link] = []
    references: list[_Link] = []
    unresolved_references = 0
    unresolved_targets = 0

    def _bind_route(route: str) -> tuple[str, dict] | None:
        """Endpoint -> Class -> raw-FQN binding for one normalized route."""
        nonlocal unresolved_targets
        endpoint = endpoints.get(route)
        if endpoint is not None:
            return endpoint, {"resolution": "endpoint"}
        fqn = bindings.get(route)
        if fqn is None:
            return None
        class_qn = _resolve_class(fqn, classes)
        if class_qn is not None:
            return class_qn, {"resolution": "class", "fqn": fqn}
        unresolved_targets += 1
        return fqn, {"resolution": "raw", "fqn": fqn, "unresolved": True}

    def _bind_bean(fqn: str) -> tuple[str, dict]:
        nonlocal unresolved_targets
        class_qn = _resolve_class(fqn, classes)
        if class_qn is not None:
            return class_qn, {"resolution": "class", "fqn": fqn}
        unresolved_targets += 1
        return fqn, {"resolution": "raw", "fqn": fqn, "unresolved": True}

    def _collect_requests(source_qn: str, text: str, patterns: tuple[re.Pattern[str], ...]) -> None:
        for pattern in patterns:
            for match in pattern.finditer(text):
                route = _normalize_url(match.group(1), config.dead_url_suffixes)
                if not route:
                    continue
                bound = _bind_route(route)
                if bound is None:
                    continue
                target, binding_extra = bound
                extra = {"route": route, "url": match.group(1), **binding_extra}
                requests.append(
                    ("REQUESTS", source_qn, target, _line_of(text, match.start()), extra)
                )

    def _collect_references(source_qn: str, text: str) -> None:
        nonlocal unresolved_references

        def _record_miss(countable: bool) -> None:
            nonlocal unresolved_references
            if countable:
                unresolved_references += 1

        def _page_link(raw: str, index: int) -> None:
            resolved, countable = _resolve_reference(
                source_qn, raw, web_root, config.context_paths, file_index,
            )
            if resolved is None:
                _record_miss(countable)
                return
            if not resolved.endswith(PAGE_SUFFIXES):
                return
            references.append((
                "REFERENCES", source_qn, resolved, _line_of(text, index),
                {"asset": "page", "href": raw},
            ))

        for match in SCRIPT_TAG.finditer(text):
            source_attr = _tag_attributes(match.group(0)).get("src")
            if not source_attr:
                continue
            resolved, countable = _resolve_reference(
                source_qn, source_attr, web_root, config.context_paths, file_index,
            )
            if resolved is None:
                _record_miss(countable)
                continue
            references.append((
                "REFERENCES", source_qn, resolved, _line_of(text, match.start()),
                {"asset": "script", "href": source_attr},
            ))

        for match in LINK_TAG.finditer(text):
            attributes = _tag_attributes(match.group(0))
            href = attributes.get("href")
            if not href or "stylesheet" not in attributes.get("rel", "").lower():
                continue
            resolved, countable = _resolve_reference(
                source_qn, href, web_root, config.context_paths, file_index,
            )
            if resolved is None:
                _record_miss(countable)
                continue
            references.append((
                "REFERENCES", source_qn, resolved, _line_of(text, match.start()),
                {"asset": "stylesheet", "href": href},
            ))

        for match in ANCHOR_HREF.finditer(text):
            _page_link(match.group(1), match.start())

        for match in FORM_ACTION.finditer(text):
            _page_link(match.group(1), match.start())

    for page_qn in jsp_pages:
        text = _read(Path(page_qn))
        if not text:
            continue

        for match in bean_pattern.finditer(text):
            target, binding_extra = _bind_bean(match.group(1))
            renders.append((
                "RENDERS", page_qn, target, _line_of(text, match.start()), binding_extra,
            ))

        _collect_requests(page_qn, text, (HREF_URL, AJAX_URL, FETCH_URL))

        for pattern in (INCLUDE_DIRECTIVE, INCLUDE_TAG):
            for match in pattern.finditer(text):
                target, _countable = _resolve_reference(
                    page_qn, match.group(1), web_root, config.context_paths, file_index,
                )
                if target is not None and target.endswith(PAGE_SUFFIXES):
                    includes.append((
                        "INCLUDES", page_qn, target, _line_of(text, match.start()),
                        {"href": match.group(1)},
                    ))

        _collect_references(page_qn, text)

    for page_qn in html_pages:
        text = _read(Path(page_qn))
        if not text:
            continue
        _collect_references(page_qn, text)

    for script_qn in scripts:
        text = _read(Path(script_qn))
        if not text:
            continue
        _collect_requests(script_qn, text, (AJAX_URL, FETCH_URL))

    deduped = {
        "RENDERS": _dedupe(renders),
        "REQUESTS": _dedupe(requests),
        "INCLUDES": _dedupe(includes),
        "REFERENCES": _dedupe(references, by_asset=True),
    }
    now = time.time()
    edge_rows = [
        (kind, source, target, source, line, json.dumps(extra, sort_keys=True), now)
        for kind, links in deduped.items()
        for _kind, source, target, line, extra in links
    ]
    conn.executemany(
        """INSERT INTO edges (kind, source_qualified, target_qualified,
           file_path, line, extra, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        edge_rows,
    )
    store._invalidate_cache()

    result = {
        "files_indexed": len(jsp_pages) + len(html_pages),
        "endpoints": len(endpoints),
        "bindings": len(bindings),
        "renders": len(deduped["RENDERS"]),
        "requests": len(deduped["REQUESTS"]),
        "includes": len(deduped["INCLUDES"]),
        "references": len(deduped["REFERENCES"]),
        "unresolved_references": unresolved_references,
        "unresolved_targets": unresolved_targets,
    }
    logger.info("JSP link resolution: %s", result)
    return result
