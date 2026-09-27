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

import bisect
import json
import logging
import os
import posixpath
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


class _Lines:
    """1-based line of a character offset, by bisecting the newline offsets."""

    def __init__(self, text: str) -> None:
        self._newlines = [match.start() for match in _NEWLINE.finditer(text)]

    def of(self, index: int) -> int:
        return bisect.bisect_left(self._newlines, index) + 1


_NEWLINE = re.compile("\n")


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


def _java_sources(repo_root: Path, source_root: Path) -> list[Path]:
    """``.java`` files under *source_root*, skipping ignored and symlinked trees."""
    from ..incremental import _load_ignore_patterns, _should_ignore

    patterns = _load_ignore_patterns(repo_root)
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(source_root):
        base = Path(directory)
        relative = base.relative_to(repo_root) if base.is_relative_to(repo_root) else None
        dirnames[:] = sorted(
            name for name in dirnames
            if not (base / name).is_symlink()
            and (relative is None or not _should_ignore((relative / name).as_posix(), patterns))
        )
        for name in sorted(filenames):
            if not name.endswith(".java"):
                continue
            if relative is not None and _should_ignore((relative / name).as_posix(), patterns):
                continue
            found.append(base / name)
    return found


def _file_key(path: str, hashes: dict[str, str | None]) -> str | None:
    """Cache key for one file's content: the graph's hash, else its stat."""
    graph_hash = hashes.get(path)
    if graph_hash:
        return graph_hash
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _binding_map(
    repo_root: Path,
    config: JspResolverConfig,
    hashes: dict[str, str | None],
    cache: dict[str, Any],
    fresh: dict[str, Any],
) -> dict[str, str]:
    """URL path -> fully qualified Java class, from route-annotated classes.

    Each file's bindings are cached under its content key in *cache*; the
    entries still in use are copied to *fresh*.
    """
    route_pattern = _route_annotation_pattern(config.route_annotations)
    bindings: dict[str, str] = {}
    source_root = repo_root / config.source_root
    if not source_root.is_dir():
        return bindings
    # The graph's Java files already follow the ignore policy; a graph that
    # holds no Java File node (hand-seeded) falls back to walking the tree.
    prefix = source_root.as_posix().rstrip("/") + "/"
    indexed = sorted(qn for qn in hashes if qn.endswith(".java") and qn.startswith(prefix))
    for path in indexed or [java.as_posix() for java in _java_sources(repo_root, source_root)]:
        key = _file_key(path, hashes)
        cached = cache.get(path)
        if key is not None and cached is not None and cached[0] == key:
            found = cached[1]
        else:
            found = _java_bindings(_read(Path(path)), config, route_pattern)
        if key is not None:
            fresh[path] = [key, found]
        for route, fqn in found:
            bindings.setdefault(route, fqn)
    return bindings


def _java_bindings(
    text: str, config: JspResolverConfig, route_pattern: re.Pattern[str],
) -> list[list[str]]:
    if not any(f"@{name}" in text for name in config.route_annotations):
        return []
    package = PACKAGE_DECL.search(text)
    klass = CLASS_DECL.search(text)
    if not package or not klass:
        return []
    fqn = f"{package.group(1)}.{klass.group(1)}"
    found = []
    for match in route_pattern.finditer(text):
        key = _normalize_url(match.group(1), config.dead_url_suffixes)
        if key:
            found.append([key, fqn])
    return found


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


# Per-file extraction cache, kept with the graph and invalidated with the
# config or this format.
_STATE_KEY = "jsp_resolver_state"
_STATE_VERSION = 1
# Pre-binding link records: (tag, raw text, line).
_Raw = list


def _extract_page(text: str, kind: str, bean_pattern: re.Pattern[str]) -> list[_Raw]:
    """The raw links of one page or script, in the order edges are emitted.

    Tags: B bean class, R request URL, I include, S script src, C stylesheet,
    P page link.
    """
    lines = _Lines(text)
    found: list[_Raw] = []
    if kind == "jsp":
        for match in bean_pattern.finditer(text):
            found.append(["B", match.group(1), lines.of(match.start())])
    request_patterns = (
        (HREF_URL, AJAX_URL, FETCH_URL) if kind == "jsp"
        else (AJAX_URL, FETCH_URL) if kind == "javascript" else ()
    )
    for pattern in request_patterns:
        for match in pattern.finditer(text):
            found.append(["R", match.group(1), lines.of(match.start())])
    if kind == "jsp":
        for pattern in (INCLUDE_DIRECTIVE, INCLUDE_TAG):
            for match in pattern.finditer(text):
                found.append(["I", match.group(1), lines.of(match.start())])
    if kind in ("jsp", "html"):
        for match in SCRIPT_TAG.finditer(text):
            source_attr = _tag_attributes(match.group(0)).get("src")
            if source_attr:
                found.append(["S", source_attr, lines.of(match.start())])
        for match in LINK_TAG.finditer(text):
            attributes = _tag_attributes(match.group(0))
            href = attributes.get("href")
            if href and "stylesheet" in attributes.get("rel", "").lower():
                found.append(["C", href, lines.of(match.start())])
        for pattern in (ANCHOR_HREF, FORM_ACTION):
            for match in pattern.finditer(text):
                found.append(["P", match.group(1), lines.of(match.start())])
    return found


def _load_state(store: GraphStore, fingerprint: str) -> dict[str, Any]:
    raw = store.get_metadata(_STATE_KEY)
    try:
        state = json.loads(raw) if raw else None
    except ValueError:
        state = None
    if (
        not isinstance(state, dict)
        or state.get("version") != _STATE_VERSION
        or state.get("config") != fingerprint
    ):
        return {"pages": {}, "java": {}}
    return state


def _resolve_jsp_links(store: GraphStore, repo_root: Path) -> dict[str, int]:
    repo_root = Path(repo_root).resolve()
    config = load_jsp_resolver_config(repo_root)
    if config is not None and not config.enabled:
        return dict(_STATS_ZERO)
    if config is None:
        config = JspResolverConfig()

    conn = store._conn  # intentional: bounded post-build maintenance pass

    # REFERENCES edges whose page File node is already gone would otherwise
    # survive every rebuild as stale rows.
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
        _write_edges(conn, [])
        store._invalidate_cache()
        return dict(_STATS_ZERO)

    hashes: dict[str, str | None] = {
        row["qualified_name"]: row["file_hash"]
        for row in conn.execute(
            "SELECT qualified_name, file_hash FROM nodes WHERE kind = 'File'"
        ).fetchall()
    }
    file_index = set(hashes)
    fingerprint = repr((config, _STATE_VERSION))
    state = _load_state(store, fingerprint)
    fresh: dict[str, Any] = {"version": _STATE_VERSION, "config": fingerprint,
                             "pages": {}, "java": {}}
    classes = _class_index(conn)
    endpoints = _endpoint_map(conn, config.dead_url_suffixes)
    bindings = _binding_map(repo_root, config, hashes, state["java"], fresh["java"])
    bean_pattern = _bean_attribute_pattern(config)
    web_root = (repo_root / config.web_root).resolve()

    def _links(qualified: str, kind: str) -> list[_Raw]:
        key = _file_key(qualified, hashes)
        cached = state["pages"].get(qualified)
        if key is not None and cached is not None and cached[0] == key:
            found = cached[1]
        else:
            text = _read(Path(qualified))
            found = _extract_page(text, kind, bean_pattern) if text else []
        if key is not None:
            fresh["pages"][qualified] = [key, found]
        return found

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

    def _bind(source_qn: str, found: list[_Raw]) -> None:
        nonlocal unresolved_references
        for tag, raw, line in found:
            if tag == "B":
                target, binding_extra = _bind_bean(raw)
                renders.append(("RENDERS", source_qn, target, line, binding_extra))
            elif tag == "R":
                route = _normalize_url(raw, config.dead_url_suffixes)
                bound = _bind_route(route) if route else None
                if bound is not None:
                    target, binding_extra = bound
                    extra = {"route": route, "url": raw, **binding_extra}
                    requests.append(("REQUESTS", source_qn, target, line, extra))
            else:
                resolved, countable = _resolve_reference(
                    source_qn, raw, web_root, config.context_paths, file_index,
                )
                if tag == "I":
                    if resolved is not None and resolved.endswith(PAGE_SUFFIXES):
                        includes.append(("INCLUDES", source_qn, resolved, line, {"href": raw}))
                elif resolved is None:
                    if countable:
                        unresolved_references += 1
                elif tag == "S":
                    references.append((
                        "REFERENCES", source_qn, resolved, line,
                        {"asset": "script", "href": raw},
                    ))
                elif tag == "C":
                    references.append((
                        "REFERENCES", source_qn, resolved, line,
                        {"asset": "stylesheet", "href": raw},
                    ))
                elif resolved.endswith(PAGE_SUFFIXES):
                    references.append((
                        "REFERENCES", source_qn, resolved, line,
                        {"asset": "page", "href": raw},
                    ))

    for page_qn in jsp_pages:
        _bind(page_qn, _links(page_qn, "jsp"))
    for page_qn in html_pages:
        _bind(page_qn, _links(page_qn, "html"))
    for script_qn in scripts:
        _bind(script_qn, _links(script_qn, "javascript"))

    deduped = {
        "RENDERS": _dedupe(renders),
        "REQUESTS": _dedupe(requests),
        "INCLUDES": _dedupe(includes),
        "REFERENCES": _dedupe(references, by_asset=True),
    }
    _write_edges(conn, [
        (kind, source, target, source, line, json.dumps(extra, sort_keys=True))
        for kind, links in deduped.items()
        for _kind, source, target, line, extra in links
    ])
    if fresh != state:
        store.set_metadata(_STATE_KEY, json.dumps(fresh, separators=(",", ":")))
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


def _write_edges(conn, wanted: list[tuple]) -> None:
    """Make this resolver's edges exactly *wanted*, touching only the difference.

    RENDERS/REQUESTS/INCLUDES have no other producer; REFERENCES does (Blade,
    HCL), so only those sourced at a page File node are this resolver's.
    """
    existing: dict[tuple, list[int]] = {}
    for row in conn.execute(
        "SELECT id, kind, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES') "
        "UNION ALL "
        "SELECT id, kind, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind = 'REFERENCES' AND source_qualified IN ("
        "SELECT qualified_name FROM nodes WHERE kind = 'File' AND language IN ('jsp', 'html'))"
    ):
        key = (row[1], row[2], row[3], row[4], row[5], row[6])
        existing.setdefault(key, []).append(row[0])
    inserts = []
    for key in wanted:
        ids = existing.get(key)
        if ids:
            ids.pop()
        else:
            inserts.append(key)
    stale = [(edge_id,) for ids in existing.values() for edge_id in ids]
    if stale:
        conn.executemany("DELETE FROM edges WHERE id = ?", stale)
    if inserts:
        now = time.time()
        conn.executemany(
            """INSERT INTO edges (kind, source_qualified, target_qualified,
               file_path, line, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(*key, now) for key in inserts],
        )
