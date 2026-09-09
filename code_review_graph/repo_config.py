"""Config-driven, per-resolver repository conventions.

Some resolvers (the JSP linker, for one) need to know a repository's own
naming conventions — where templates live, which annotations mark a route —
before they can extract anything meaningful. Those conventions vary per
repository and must never be guessed at or hardcoded for one organisation.

A repo opts in by dropping ``.code-review-graph/config.toml``::

    [resolvers.jsp]
    web_root = "web"
    source_root = "src"
    route_annotations = ["UrlBinding", "RequestMapping"]
    bean_attribute = "beanclass"
    bean_package_prefix = "com."
    dead_url_suffixes = [".action"]

Sits beside the ``languages.toml`` loader in :mod:`custom_languages` and
follows the same rules: cached on ``(mtime_ns, size)``, and a broken or
missing file never raises — it is logged with ``logger.warning`` and treated
as absent. Missing keys within a present ``[resolvers.jsp]`` table fall back
to the defaults shown above (all are generic, framework-level conventions,
not specific to any organisation). A resolver whose section is entirely
absent must do nothing rather than guess.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Location of the config file, relative to the repo root.
CONFIG_RELATIVE_PATH = Path(".code-review-graph") / "config.toml"


@dataclass(frozen=True)
class JspResolverConfig:
    """Validated ``[resolvers.jsp]`` table, with defaults for omitted keys."""

    web_root: str = "web"
    source_root: str = "src"
    route_annotations: tuple[str, ...] = ("UrlBinding", "RequestMapping")
    bean_attribute: str = "beanclass"
    bean_package_prefix: str = "com."
    dead_url_suffixes: tuple[str, ...] = (".action",)


@dataclass(frozen=True)
class _CacheEntry:
    mtime_ns: int
    size: int
    jsp: Optional[JspResolverConfig] = None


_cache_lock = threading.Lock()
_cache: dict[str, _CacheEntry] = {}


def clear_cache() -> None:
    """Drop the loader cache (used by tests)."""
    with _cache_lock:
        _cache.clear()


def load_jsp_resolver_config(repo_root: Path) -> Optional[JspResolverConfig]:
    """Load ``[resolvers.jsp]`` from ``<repo_root>/.code-review-graph/config.toml``.

    Returns ``None`` when the file is missing, unreadable, malformed, or has
    no ``[resolvers.jsp]`` table — the caller must treat that as "do nothing",
    never as "use the defaults". Returns a populated config, with per-key
    defaults filled in, when the table is present.
    """
    config_path = Path(repo_root) / CONFIG_RELATIVE_PATH
    try:
        stat = config_path.stat()
    except OSError:
        return None  # No config file — the common case; not worth a log line.

    cache_key = str(config_path)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if (
            cached is not None
            and cached.mtime_ns == stat.st_mtime_ns
            and cached.size == stat.st_size
        ):
            return cached.jsp

    jsp = _load_uncached(config_path)
    with _cache_lock:
        _cache[cache_key] = _CacheEntry(stat.st_mtime_ns, stat.st_size, jsp)
    return jsp


def _load_uncached(config_path: Path) -> Optional[JspResolverConfig]:
    if tomllib is None:
        logger.warning(
            "%s found but TOML parsing requires the 'tomli' package on "
            "Python < 3.11 — resolvers.jsp not loaded",
            config_path,
        )
        return None
    try:
        raw = config_path.read_bytes()
    except (OSError, PermissionError) as exc:
        logger.warning("Cannot read %s: %s — resolvers.jsp not loaded", config_path, exc)
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Malformed TOML in %s: %s — resolvers.jsp not loaded", config_path, exc)
        return None

    resolvers = data.get("resolvers")
    if resolvers is None:
        return None
    if not isinstance(resolvers, dict):
        logger.warning(
            "%s: [resolvers] must be a table of tables — resolvers.jsp not loaded",
            config_path,
        )
        return None
    table = resolvers.get("jsp")
    if table is None:
        return None
    return _validate_jsp_table(table, config_path)


def _validate_jsp_table(table: object, config_path: Path) -> Optional[JspResolverConfig]:
    """Validate one ``[resolvers.jsp]`` table.

    Any single malformed key invalidates the whole table (logged once) rather
    than silently mixing defaults with a half-trusted config — matching how
    ``custom_languages`` treats a broken entry.
    """
    if not isinstance(table, dict):
        logger.warning(
            "%s: [resolvers.jsp] is not a table — resolvers.jsp not loaded",
            config_path,
        )
        return None

    defaults = JspResolverConfig()
    values: dict[str, object] = {}

    for key in ("web_root", "source_root", "bean_attribute", "bean_package_prefix"):
        if key not in table:
            continue
        value = table[key]
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "%s: resolvers.jsp.%s must be a non-empty string — resolvers.jsp not loaded",
                config_path, key,
            )
            return None
        values[key] = value.strip()

    for key in ("route_annotations", "dead_url_suffixes"):
        if key not in table:
            continue
        value = table[key]
        if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            logger.warning(
                "%s: resolvers.jsp.%s must be a non-empty list of non-empty "
                "strings — resolvers.jsp not loaded",
                config_path, key,
            )
            return None
        values[key] = tuple(item.strip() for item in value)

    return JspResolverConfig(
        web_root=values.get("web_root", defaults.web_root),
        source_root=values.get("source_root", defaults.source_root),
        route_annotations=values.get("route_annotations", defaults.route_annotations),
        bean_attribute=values.get("bean_attribute", defaults.bean_attribute),
        bean_package_prefix=values.get("bean_package_prefix", defaults.bean_package_prefix),
        dead_url_suffixes=values.get("dead_url_suffixes", defaults.dead_url_suffixes),
    )
