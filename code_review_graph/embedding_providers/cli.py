"""``code-review-graph embeddings enable|disable|status``.

``enable`` writes ``[embeddings]`` into the tracked ``.code-review-graph.toml``
(other content is left as it is) and, when a graph exists, builds the first
vectors under the writer lock. On Apple Silicon it first checks MLX against
the ONNX reference and records the result for this machine.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from ..locking import EXIT_DEGRADED, EXIT_ERROR, EXIT_OK, EXIT_USAGE
from ..repo_settings import (
    CLOUD_PROVIDER_PROFILES,
    DEFAULT_EMBEDDING_PROFILE,
    EMBEDDING_PROFILES,
    EmbeddingSettings,
    SettingsWriteError,
    get_section,
    load_embedding_settings,
    write_section,
)

LockEnterer = Callable[[contextlib.ExitStack, argparse.Namespace, Path], None]


def register(
    subparsers: Any,
    add_lock_args: Optional[Callable[[argparse.ArgumentParser], None]] = None,
    enter_lock: Optional[LockEnterer] = None,
) -> argparse.ArgumentParser:
    """Add ``embeddings`` to the main CLI; dispatch with ``args.embeddings_run(args)``."""
    parser = subparsers.add_parser(
        "embeddings", help="Turn semantic search on or off and show its state",
    )
    sub = parser.add_subparsers(dest="embeddings_command", required=True)

    enable = sub.add_parser("enable", help="Enable embeddings and build the first vectors")
    enable.add_argument(
        "--profile", choices=EMBEDDING_PROFILES + CLOUD_PROVIDER_PROFILES, default=None,
        help=f"fast | balanced | accurate | legacy (default: the configured profile, "
             f"else {DEFAULT_EMBEDDING_PROFILE}); cloud provider names are accepted too",
    )
    enable.add_argument(
        "--no-embed", action="store_true",
        help="Only write the setting; vectors are built by the next build or update",
    )

    disable = sub.add_parser("disable", help="Disable embeddings (vectors are kept)")
    disable.add_argument("--purge", action="store_true", help="Also delete stored vectors")

    status = sub.add_parser("status", help="Backend, model, vectors and state")
    status.add_argument("--json", action="store_true", help="JSON output")

    for command in (enable, disable, status):
        command.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    for command in (enable, disable):
        command.add_argument("--json", action="store_true", help="JSON output")
        if add_lock_args is not None:
            add_lock_args(command)
    parser.set_defaults(embeddings_run=lambda args: run(args, enter_lock))
    return parser


def _emit(args: argparse.Namespace, payload: dict[str, Any], lines: list[str]) -> None:
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        for line in lines:
            print(line)


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def check_mlx_parity(db_path: Path, settings: EmbeddingSettings) -> Optional[dict[str, Any]]:
    """Compare MLX with the ONNX reference for ``balanced`` and record the result.

    Returns the recorded result, or ``None`` when the check does not apply
    (not Apple Silicon, another profile, or a backend is not installed).
    """
    from ..embeddings import MLX_PARITY_KEY, write_embeddings_meta
    from .backends import FastEmbedProvider, MlxEmbeddingsProvider, parity_cosine
    from .profiles import (
        BALANCED_MLX,
        BALANCED_ONNX,
        MAC_ARM,
        PARITY_MIN_COSINE,
        module_available,
        platform_key,
    )

    if settings.profile != "balanced" or platform_key() != MAC_ARM:
        return None
    if not (module_available(BALANCED_MLX.module) and module_available(BALANCED_ONNX.module)):
        return None
    dim = settings.dim or BALANCED_MLX.default_dim
    model = settings.model or BALANCED_MLX.model
    record: dict[str, Any] = {"model": model, "reference": BALANCED_ONNX.model,
                              "platform": MAC_ARM, "dim": dim}
    candidate = MlxEmbeddingsProvider(BALANCED_MLX, model, dim, idle_unload_s=0)
    reference = FastEmbedProvider(BALANCED_ONNX, BALANCED_ONNX.model, dim,
                                  threads=settings.effective_threads, idle_unload_s=0)
    try:
        cosine = parity_cosine(candidate, reference)
        record.update(cosine=round(cosine, 5), ok=cosine >= PARITY_MIN_COSINE)
    except Exception as exc:  # any backend failure means "do not use MLX"
        record.update(cosine=None, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        candidate.unload()
        reference.unload()
    import sqlite3

    with contextlib.closing(sqlite3.connect(str(db_path), timeout=30,
                                            isolation_level=None)) as conn:
        write_embeddings_meta(conn, {MLX_PARITY_KEY: json.dumps(record, sort_keys=True)})
    return record


def run(args: argparse.Namespace, enter_lock: Optional[LockEnterer] = None) -> int:
    from ..incremental import find_project_root, get_db_path

    repo_root = Path(args.repo).expanduser().resolve() if args.repo else find_project_root()
    command = args.embeddings_command
    try:
        if command == "status":
            return _status(args, repo_root, get_db_path(repo_root, read_only=True))
        if command == "enable":
            return _enable(args, repo_root, get_db_path(repo_root, read_only=True), enter_lock)
        if command == "disable":
            return _disable(args, repo_root, get_db_path(repo_root, read_only=True), enter_lock)
    except (SettingsWriteError, OSError, ValueError) as exc:
        print(f"code-review-graph embeddings: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_USAGE


def _status(args: argparse.Namespace, repo_root: Path, db_path: Path) -> int:
    from ..embeddings import embeddings_status

    status = embeddings_status(db_path, repo_root)
    lines = [
        f"Embeddings: {status['state']}"
        + ("" if status["enabled"] else " (enable: code-review-graph embeddings enable)"),
        f"  profile:  {status['profile']} (from {status['settings_source']})",
        f"  backend:  {status['backend']}",
        f"  model:    {status['model']}",
        f"  dim:      {status['dim']} ({status['dtype']})",
        f"  vectors:  {status['vectors']} for this provider, "
        f"{status['vectors_all_providers']} stored",
        f"  stale:    {status['stale_count']}",
        f"  disk:     {_human_bytes(status['disk_bytes'])}",
    ]
    if status.get("warning"):
        lines.append(f"  warning:  {status['warning']}")
    _emit(args, status, lines)
    return EXIT_OK


def _locked(stack: contextlib.ExitStack, args: argparse.Namespace, db_path: Path,
            enter_lock: Optional[LockEnterer]) -> None:
    if enter_lock is not None:
        enter_lock(stack, args, db_path)
    else:
        from ..locking import writer_lock

        stack.enter_context(writer_lock(db_path, wait=30.0))


def _enable(args: argparse.Namespace, repo_root: Path, db_path: Path,
            enter_lock: Optional[LockEnterer]) -> int:
    configured = get_section(repo_root, "embeddings") or {}
    profile = args.profile or configured.get("profile") or DEFAULT_EMBEDDING_PROFILE
    path = write_section(repo_root, "embeddings", {"enabled": True, "profile": profile})
    settings = load_embedding_settings(repo_root)
    payload: dict[str, Any] = {"settings_file": str(path), "profile": profile}
    lines = [f"Enabled embeddings (profile {profile}) in {path}"]
    if not settings.enabled:
        note = (f"CRG_EMBEDDINGS={os.environ.get('CRG_EMBEDDINGS')} overrides the file; "
                "embeddings stay off in this environment")
        payload["warning"] = note
        _emit(args, payload, lines + [f"  warning: {note}"])
        return EXIT_DEGRADED
    if args.no_embed or not db_path.exists():
        if not db_path.exists():
            lines.append("  no graph yet: vectors are built after `code-review-graph build`")
        _emit(args, payload, lines)
        return EXIT_OK

    from ..embeddings import embed_changed
    from ..graph import GraphStore

    with contextlib.ExitStack() as stack:
        _locked(stack, args, db_path, enter_lock)
        parity = check_mlx_parity(db_path, settings)
        if parity is not None:
            payload["mlx_parity"] = parity
            verdict = "passed, using MLX" if parity.get("ok") else "failed, using ONNX"
            lines.append(f"  MLX parity vs ONNX: cosine {parity.get('cosine')} ({verdict})")
        store = GraphStore(db_path)
        stack.callback(store.close)
        result = embed_changed(store, None, repo_root=repo_root)
    assert isinstance(result, dict)
    payload["result"] = result
    lines.append(
        f"  {result['state']}: {result.get('embedded', 0)} embedded, "
        f"{result.get('stale_count', 0)} stale ({result.get('provider') or 'no provider'})"
    )
    for key in ("warning", "error"):
        if result.get(key):
            lines.append(f"  {key}: {result[key]}")
    _emit(args, payload, lines)
    return EXIT_OK if result["state"] == "ready" else EXIT_DEGRADED


def _disable(args: argparse.Namespace, repo_root: Path, db_path: Path,
             enter_lock: Optional[LockEnterer]) -> int:
    path = write_section(repo_root, "embeddings", {"enabled": False})
    payload: dict[str, Any] = {"settings_file": str(path), "purged": 0}
    lines = [f"Disabled embeddings in {path}"]
    if db_path.exists():
        import sqlite3

        from ..embeddings import purge_vectors, write_embeddings_meta

        with contextlib.ExitStack() as stack:
            _locked(stack, args, db_path, enter_lock)
            if args.purge:
                payload["purged"] = purge_vectors(db_path)
                lines.append(f"  deleted {payload['purged']} vectors")
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=30,
                                                    isolation_level=None)) as conn:
                write_embeddings_meta(conn, {"embeddings_state": "off"})
    if os.environ.get("CRG_EMBEDDINGS", "").strip().lower() not in ("", "off", "0", "false"):
        lines.append(f"  note: CRG_EMBEDDINGS={os.environ['CRG_EMBEDDINGS']} still enables "
                     "embeddings in this environment")
    _emit(args, payload, lines)
    return EXIT_OK

