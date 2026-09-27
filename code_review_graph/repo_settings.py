"""Tracked repository settings in ``<repo_root>/.code-review-graph.toml``.

One loader for every section of the file: ``[embeddings]`` today, and
``[resolvers.*]`` tables next (they fall back to the legacy, untracked
``.code-review-graph/config.toml`` that :mod:`repo_config` reads)::

    [embeddings]
    enabled = false          # off by default; `code-review-graph embeddings enable`
    profile = "balanced"     # fast | balanced | accurate | legacy (or a cloud provider)
    # model = "..."          # override the profile's model
    # dim = 256              # vector size (MRL-truncated where the model supports it)
    dtype = "float16"        # stored vector dtype: float16 | float32
    batch_size = 64
    # threads = 4            # default: half the cores
    idle_unload_s = 600      # release the model after this many idle seconds

Environment overrides: ``CRG_EMBEDDINGS=off|on|fast|balanced|accurate|legacy``
(or a cloud provider name), ``CRG_EMBEDDING_DIM`` and ``CRG_EMBEDDING_MODEL``.
``CRG_EMBEDDING_MODEL`` keeps its historic meaning, a sentence-transformers
model, so it only applies to the ``legacy`` and ``local`` profiles.

A missing or broken file never raises: it is logged once and read as empty.
Parsed files are cached on ``(mtime_ns, size)``.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = ".code-review-graph.toml"
LEGACY_CONFIG_RELATIVE_PATH = Path(".code-review-graph") / "config.toml"

EMBEDDING_PROFILES = ("fast", "balanced", "accurate", "legacy")
CLOUD_PROVIDER_PROFILES = ("local", "openai", "google", "minimax", "voyage")
DEFAULT_EMBEDDING_PROFILE = "balanced"
EMBEDDING_DTYPES = ("float16", "float32")

_OFF_VALUES = {"off", "0", "false", "no", "none", "disabled"}
_ON_VALUES = {"on", "1", "true", "yes", "enabled"}
# CRG_EMBEDDING_MODEL names a sentence-transformers model (harness adapters
# set it globally), so it must not leak into the model2vec/ONNX/MLX profiles.
_ENV_MODEL_PROFILES = {"legacy", "local"}

_cache_lock = threading.Lock()
_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}


def clear_cache() -> None:
    """Drop the parsed-file cache (used by tests and after writes)."""
    with _cache_lock:
        _cache.clear()


def settings_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / SETTINGS_FILENAME


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = str(path)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    data: dict[str, Any] = {}
    if tomllib is None:
        logger.warning("%s found but TOML parsing needs 'tomli' on Python < 3.11", path)
    else:
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8", errors="replace"))
        except OSError as exc:
            logger.warning("Cannot read %s: %s; settings ignored", path, exc)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("Malformed TOML in %s: %s; settings ignored", path, exc)
    with _cache_lock:
        _cache[key] = (stat.st_mtime_ns, stat.st_size, data)
    return data


def load_settings(repo_root: str | Path | None) -> dict[str, Any]:
    """The parsed ``.code-review-graph.toml`` of *repo_root* (``{}`` when absent)."""
    if repo_root is None:
        return {}
    return _load_toml(settings_path(repo_root))


def get_section(repo_root: str | Path | None, dotted: str) -> Optional[dict[str, Any]]:
    """One table by dotted name (``"embeddings"``, ``"resolvers.jsp"``) or ``None``."""
    node: Any = load_settings(repo_root)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def resolver_table(
    repo_root: str | Path, name: str,
) -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    """``[resolvers.<name>]`` and the file it came from.

    The tracked settings file wins; the legacy ``.code-review-graph/config.toml``
    is read only when the tracked file has no such table.
    """
    table = get_section(repo_root, f"resolvers.{name}")
    if table is not None:
        return table, settings_path(repo_root)
    legacy_path = Path(repo_root) / LEGACY_CONFIG_RELATIVE_PATH
    resolvers = _load_toml(legacy_path).get("resolvers")
    if isinstance(resolvers, dict) and isinstance(resolvers.get(name), dict):
        return resolvers[name], legacy_path
    return None, None


# ---------------------------------------------------------------------------
# [embeddings]
# ---------------------------------------------------------------------------


def default_threads() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool = False
    profile: str = DEFAULT_EMBEDDING_PROFILE
    model: Optional[str] = None
    dim: Optional[int] = None
    dtype: str = "float16"
    batch_size: int = 64
    threads: int = 0  # 0: half the cores (see ``effective_threads``)
    idle_unload_s: float = 600.0
    # Where ``enabled``/``profile`` came from: "default", "file" or "env".
    source: str = "default"

    @property
    def effective_threads(self) -> int:
        return self.threads if self.threads > 0 else default_threads()


def _known_profile(value: str) -> bool:
    return value in EMBEDDING_PROFILES or value in CLOUD_PROVIDER_PROFILES


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _from_table(table: Mapping[str, Any], origin: str) -> EmbeddingSettings:
    settings = EmbeddingSettings()
    changes: dict[str, Any] = {}
    enabled = table.get("enabled")
    if isinstance(enabled, bool):
        changes["enabled"] = enabled
        changes["source"] = "file"
    elif enabled is not None:
        logger.warning("%s: embeddings.enabled must be a boolean; ignored", origin)
    profile = table.get("profile")
    if isinstance(profile, str) and _known_profile(profile.strip().lower()):
        changes["profile"] = profile.strip().lower()
    elif profile is not None:
        logger.warning("%s: unknown embeddings.profile %r; using default", origin, profile)
    model = table.get("model")
    if isinstance(model, str) and model.strip():
        changes["model"] = model.strip()
    for key in ("dim", "batch_size", "threads"):
        if key in table:
            number = _positive_int(table[key])
            if number is None:
                logger.warning("%s: embeddings.%s must be a positive integer; ignored",
                               origin, key)
            else:
                changes[key] = number
    dtype = table.get("dtype")
    if isinstance(dtype, str) and dtype.strip().lower() in EMBEDDING_DTYPES:
        changes["dtype"] = dtype.strip().lower()
    elif dtype is not None:
        logger.warning("%s: embeddings.dtype must be float16 or float32; ignored", origin)
    idle = table.get("idle_unload_s")
    if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle >= 0:
        changes["idle_unload_s"] = float(idle)
    elif idle is not None:
        logger.warning("%s: embeddings.idle_unload_s must be >= 0; ignored", origin)
    return replace(settings, **changes)


def _apply_env(settings: EmbeddingSettings, env: Mapping[str, str]) -> EmbeddingSettings:
    changes: dict[str, Any] = {}
    raw = env.get("CRG_EMBEDDINGS", "").strip().lower()
    if raw:
        if raw in _OFF_VALUES:
            changes.update(enabled=False, source="env")
        elif raw in _ON_VALUES:
            changes.update(enabled=True, source="env")
        elif _known_profile(raw):
            changes.update(enabled=True, profile=raw, source="env")
        else:
            logger.warning("Unknown CRG_EMBEDDINGS=%r; ignored", raw)
    dim_raw = env.get("CRG_EMBEDDING_DIM", "").strip()
    if dim_raw:
        dim = _positive_int(dim_raw)
        if dim is None:
            logger.warning("CRG_EMBEDDING_DIM=%r is not a positive integer; ignored", dim_raw)
        else:
            changes["dim"] = dim
    settings = replace(settings, **changes)
    env_model = env.get("CRG_EMBEDDING_MODEL", "").strip()
    if env_model and settings.profile in _ENV_MODEL_PROFILES:
        settings = replace(settings, model=env_model)
    return settings


def load_embedding_settings(
    repo_root: str | Path | None, env: Optional[Mapping[str, str]] = None,
) -> EmbeddingSettings:
    """``[embeddings]`` of *repo_root* with environment overrides applied."""
    table = get_section(repo_root, "embeddings") or {}
    origin = str(settings_path(repo_root)) if repo_root is not None else SETTINGS_FILENAME
    return _apply_env(_from_table(table, origin), os.environ if env is None else env)


# ---------------------------------------------------------------------------
# Writer: minimal line edits, so comments and other tables survive
# ---------------------------------------------------------------------------


class SettingsWriteError(RuntimeError):
    pass


def _toml_value(value: Any) -> str:
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are valid TOML basic-string escapes.
        return json.dumps(value)
    raise SettingsWriteError(f"unsupported settings value: {value!r}")


def _is_header(line: str) -> bool:
    return line.lstrip().startswith("[")


def _header_name(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    end = stripped.find("]")
    if end < 0:
        return None
    rest = stripped[end + 1:].strip()
    if rest and not rest.startswith("#"):
        return None
    return stripped[1:end].strip()


def _line_key(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().strip('"').strip("'")


def _trailing_comment(line: str) -> str:
    """The ``# ...`` tail of a ``key = value`` line, found by re-parsing."""
    if tomllib is None:
        return ""
    for pos, char in enumerate(line):
        if char != "#":
            continue
        try:
            tomllib.loads(line[:pos])
        except tomllib.TOMLDecodeError:
            continue
        return "  " + line[pos:].rstrip("\n")
    return ""


def update_section(text: str, section: str, values: Mapping[str, Any]) -> str:
    """Return *text* with ``[section]`` keys set (``None`` removes a key).

    Other tables, comments and key order are left as they are. Raises
    :class:`SettingsWriteError` when the section is spelled in a form this
    editor does not rewrite (dotted keys or an inline table).
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next(
        (i for i, line in enumerate(lines) if _header_name(line) == section), None,
    )
    if start is None:
        if tomllib is not None and text.strip():
            try:
                if section in tomllib.loads(text):
                    raise SettingsWriteError(
                        f"[{section}] is written as dotted keys or an inline table; "
                        "edit it by hand",
                    )
            except tomllib.TOMLDecodeError as exc:
                raise SettingsWriteError(f"cannot parse existing settings: {exc}") from exc
        new = [f"{key} = {_toml_value(value)}\n" for key, value in values.items()
               if value is not None]
        prefix = "".join(lines)
        sep = "\n" if prefix.strip() else ""
        return prefix + sep + f"[{section}]\n" + "".join(new)

    end = next((i for i in range(start + 1, len(lines)) if _is_header(lines[i])), len(lines))
    body = lines[start + 1:end]
    pending = dict(values)
    out: list[str] = []
    for line in body:
        key = _line_key(line)
        if key is not None and key in pending:
            value = pending.pop(key)
            if value is None:
                continue
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{key} = {_toml_value(value)}{_trailing_comment(line)}\n")
        else:
            out.append(line)
    additions = [f"{key} = {_toml_value(value)}\n" for key, value in pending.items()
                 if value is not None]
    if additions:
        # Insert after the last non-blank line so the gap before the next table stays.
        insert_at = len(out)
        while insert_at > 0 and not out[insert_at - 1].strip():
            insert_at -= 1
        out[insert_at:insert_at] = additions
    return "".join(lines[: start + 1] + out + lines[end:])


def write_section(repo_root: str | Path, section: str, values: Mapping[str, Any]) -> Path:
    """Apply :func:`update_section` to the repo's settings file, atomically."""
    path = settings_path(repo_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    updated = update_section(text, section, values)
    if tomllib is not None:
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
            raise SettingsWriteError(f"refusing to write invalid TOML: {exc}") from exc
    if updated != text:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".crg-settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    clear_cache()
    return path


__all__ = [
    "CLOUD_PROVIDER_PROFILES",
    "DEFAULT_EMBEDDING_PROFILE",
    "EMBEDDING_PROFILES",
    "EmbeddingSettings",
    "SETTINGS_FILENAME",
    "SettingsWriteError",
    "clear_cache",
    "get_section",
    "load_embedding_settings",
    "load_settings",
    "resolver_table",
    "settings_path",
    "update_section",
    "write_section",
]
