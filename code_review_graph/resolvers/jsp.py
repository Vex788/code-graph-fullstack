"""Post-build resolution of JSP <-> Java cross-language links.

No bundled tree-sitter grammar covers JSP, so JSP pages are invisible to the
parser: a template-heavy Java web app can have hundreds of pages and zero
graph nodes for them, and a reviewer cannot see which page a change breaks.
This resolver recovers three cross-language links with a plain text scan and
creates the missing File nodes itself — it is the sole producer of
``language = 'jsp'`` nodes.

  RENDERS    jsp -> Java class    (an explicit bean-binding attribute)
  REQUESTS   jsp/js -> Java class (an href/action/url/ajax/fetch call to a
                                    route-annotated endpoint)
  INCLUDES   jsp -> jsp           (``<%@ include %>`` / ``<jsp:include>``)

Entirely config-driven (see :mod:`code_review_graph.repo_config`) so it never
guesses at one repository's routing conventions: with no ``[resolvers.jsp]``
section it does nothing. Targets are the dotted Java FQN read straight off
the route annotation (``package.Class``), not a graph node's own
``file::Class`` qualified name — matching the source data actually available
from a text scan, not a resolved graph identity.

Idempotent: it deletes its own rows first, so a full rebuild and an
incremental update converge to the same graph.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..repo_config import JspResolverConfig, load_jsp_resolver_config

if TYPE_CHECKING:
    from ..graph import GraphStore

logger = logging.getLogger(__name__)

JSP_SUFFIXES = (".jsp", ".jspf", ".tag")

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

_STATS_ZERO = {
    "files_indexed": 0,
    "bindings": 0,
    "renders": 0,
    "requests": 0,
    "includes": 0,
}


def _excluded(path: Path) -> bool:
    text = path.as_posix()
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


def _jsp_files(web_root: Path) -> list[Path]:
    if not web_root.is_dir():
        return []
    return sorted(
        p for p in web_root.rglob("*")
        if p.suffix in JSP_SUFFIXES and p.is_file() and not _excluded(p)
    )


def _resolve_include(current: Path, raw: str, web_root: Path) -> Path | None:
    if "${" in raw or "<%" in raw:
        return None
    target = raw.split("?", 1)[0]
    candidate = (web_root / target.lstrip("/")) if target.startswith("/") else (current.parent / target)
    try:
        return candidate.resolve()
    except OSError:
        return None


def _dedupe(links: list[tuple[str, str, Path, int]]) -> list[tuple[str, str, Path, int]]:
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str, Path, int]] = []
    for link in links:
        key = (link[0], link[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def _rel(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root).as_posix()


def resolve_jsp_links(store: GraphStore, repo_root: Path) -> dict[str, int]:
    """Extract JSP File nodes and RENDERS/REQUESTS/INCLUDES edges.

    Returns a zero-stats dict, without touching the database, when the repo
    has no ``[resolvers.jsp]`` config section — never guesses at conventions
    an unconfigured repo hasn't declared.
    """
    repo_root = Path(repo_root).resolve()
    config = load_jsp_resolver_config(repo_root)
    if config is None:
        return dict(_STATS_ZERO)

    conn = store._conn  # intentional: bounded post-build maintenance pass

    # Idempotent: clear this resolver's own rows before recomputing them, so
    # a full rebuild and an incremental update converge to the same graph.
    conn.execute("DELETE FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES')")
    conn.execute("DELETE FROM nodes WHERE kind = 'File' AND language = 'jsp'")

    web_root = repo_root / config.web_root
    pages = _jsp_files(web_root)
    if not pages:
        conn.commit()
        store._invalidate_cache()
        return dict(_STATS_ZERO)

    bindings = _binding_map(repo_root, config)
    bean_pattern = _bean_attribute_pattern(config)

    renders: list[tuple[str, str, Path, int]] = []
    requests: list[tuple[str, str, Path, int]] = []
    includes: list[tuple[str, str, Path, int]] = []

    now = time.time()
    for page in pages:
        rel = _rel(page, repo_root)
        text = _read(page)
        max_line = _line_of(text, len(text))
        conn.execute(
            """INSERT INTO nodes
               (kind, name, qualified_name, file_path, line_start, line_end,
                language, is_test, extra, updated_at)
               VALUES ('File', ?, ?, ?, 1, ?, 'jsp', 0, '{}', ?)
               ON CONFLICT(qualified_name) DO UPDATE SET
                 line_end = excluded.line_end, updated_at = excluded.updated_at""",
            (rel, rel, rel, max_line, now),
        )

        for match in bean_pattern.finditer(text):
            renders.append((rel, match.group(1), page, _line_of(text, match.start())))

        for pattern in (HREF_URL, AJAX_URL, FETCH_URL):
            for match in pattern.finditer(text):
                fqn = bindings.get(_normalize_url(match.group(1), config.dead_url_suffixes))
                if fqn:
                    requests.append((rel, fqn, page, _line_of(text, match.start())))

        for pattern in (INCLUDE_DIRECTIVE, INCLUDE_TAG):
            for match in pattern.finditer(text):
                target = _resolve_include(page, match.group(1), web_root)
                if target is not None and target.exists():
                    includes.append(
                        (rel, _rel(target, repo_root), page, _line_of(text, match.start()))
                    )

    for script in sorted(web_root.rglob("*.js")):
        if _excluded(script) or not script.is_file():
            continue
        text = _read(script)
        rel = _rel(script, repo_root)
        for pattern in (AJAX_URL, FETCH_URL):
            for match in pattern.finditer(text):
                fqn = bindings.get(_normalize_url(match.group(1), config.dead_url_suffixes))
                if fqn:
                    requests.append((rel, fqn, script, _line_of(text, match.start())))

    deduped = {
        "RENDERS": _dedupe(renders),
        "REQUESTS": _dedupe(requests),
        "INCLUDES": _dedupe(includes),
    }
    edge_rows = [
        (kind, source, target, _rel(file_path, repo_root), line, now)
        for kind, links in deduped.items()
        for source, target, file_path, line in links
    ]

    conn.executemany(
        """INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        edge_rows,
    )
    conn.commit()
    store._invalidate_cache()

    result = {
        "files_indexed": len(pages),
        "bindings": len(bindings),
        "renders": len(deduped["RENDERS"]),
        "requests": len(deduped["REQUESTS"]),
        "includes": len(deduped["INCLUDES"]),
    }
    logger.info("JSP link resolution: %s", result)
    return result
