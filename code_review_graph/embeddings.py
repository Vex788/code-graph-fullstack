"""Vector embedding support for semantic code search.

Supports multiple providers:
1. Local (sentence-transformers) - Private, fast, offline.
2. Google Gemini - High-quality, cloud-based. Requires explicit opt-in.
3. MiniMax (embo-01) - High-quality 1536-dim cloud embeddings. Requires MINIMAX_API_KEY.
4. OpenAI-compatible - Any endpoint speaking OpenAI /v1/embeddings (real OpenAI,
   Azure OpenAI, self-hosted gateways like new-api / LiteLLM / vLLM / LocalAI / Ollama).
5. Voyage AI - Code retrieval embeddings via the Voyage embeddings API.

Embeddings are off by default. ``code-review-graph embeddings enable`` (or
``CRG_EMBEDDINGS=<profile>``) selects a profile from
:mod:`code_review_graph.embedding_providers`: ``fast`` (model2vec),
``balanced`` (EmbeddingGemma on MLX or ONNX), ``accurate`` (Qwen3 on MLX) or
``legacy`` (sentence-transformers). Vectors are stored L2-normalized as
float16; older float32 rows are still read.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import logging
import math
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional
from urllib.parse import urlparse

from . import __version__ as _crg_version
from .embedding_providers.profiles import Resolution, resolve_profile
from .graph import GraphNode, GraphStore, node_to_dict
from .repo_settings import EmbeddingSettings, load_embedding_settings

if TYPE_CHECKING:
    from google.genai.types import ContentListUnion

logger = logging.getLogger(__name__)

# Sent on every cloud-provider HTTP request. Some providers (e.g. Fireworks)
# sit behind Cloudflare and reject the urllib default ``Python-urllib/X.Y``
# UA with HTTP 403 / error 1010 ("browser signature banned"). A real UA gets
# us through and gives upstream a way to identify CRG-driven traffic.
_USER_AGENT = (
    f"code-review-graph/{_crg_version} "
    "(+https://github.com/tirth8205/code-review-graph)"
)

# ---------------------------------------------------------------------------
# Provider Interface and Implementations
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents (indexed node text)."""
        pass

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Document-side embedding; the index always goes through this."""
        return self.embed(texts)

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (may use a different task type or prompt than indexing)."""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


LOCAL_DEFAULT_MODEL = "all-MiniLM-L6-v2"


# Process-wide cache and initialization lock for sentence-transformer models.
# The dependency import itself touches process-global Torch state, so one lock
# must cover availability checks, imports, and model construction across every
# model name. A per-model lock would still allow two first imports to race.
# Populated by ``prewarm_local_embeddings()`` at server startup (see ``main.main``)
# and by ``LocalEmbeddingProvider._get_model`` on first lazy load. Sharing the
# loaded model across ``LocalEmbeddingProvider`` instances avoids re-importing
# ``sentence_transformers`` + ``torch`` from worker threads, which deadlocks
# ``semantic_search_nodes_tool`` on Windows stdio MCP (#385 fixed the peer
# tools via ``asyncio.to_thread``; this cache fixes the remaining case where
# torch DLL / OpenMP init runs inside an executor thread).
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_INIT_LOCK = threading.RLock()


def prewarm_local_embeddings(model_name: str | None = None) -> None:
    """Eagerly load the local sentence-transformer model on the calling thread.

    Call this from the **main thread** before entering an asyncio event loop
    (e.g. before ``mcp.run()``) on Windows to prevent a deadlock where lazy-
    loading ``sentence_transformers`` + ``torch`` inside a FastMCP executor
    worker thread blocks indefinitely on DLL init / OpenMP thread-pool
    registration.

    No-op when ``sentence-transformers`` is not installed (cloud-provider
    setups remain unaffected) or when the configured model is already cached.

    Args:
        model_name: Optional override; falls back to the ``CRG_EMBEDDING_MODEL``
            environment variable and then to ``LOCAL_DEFAULT_MODEL``.
    """
    resolved = model_name or os.environ.get(
        "CRG_EMBEDDING_MODEL", LOCAL_DEFAULT_MODEL
    )
    try:
        LocalEmbeddingProvider(resolved)._get_model()
    except ImportError:
        return  # cloud-only setup: nothing to pre-warm
    except Exception as exc:  # pragma: no cover — best-effort startup hook
        logger.warning("prewarm_local_embeddings(%s) skipped: %s", resolved, exc)


class LocalEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.environ.get(
            "CRG_EMBEDDING_MODEL", LOCAL_DEFAULT_MODEL
        )
        self._model = None  # Lazy-loaded

    def _get_model(self):
        if self._model is not None:
            return self._model

        # Fast path for a model fully published by another provider instance.
        cached = _MODEL_CACHE.get(self._model_name)
        if cached is not None:
            self._model = cached
            return self._model

        with _MODEL_INIT_LOCK:
            # A competing caller may have initialized this provider or cache
            # entry while we waited. Recheck both under the process-wide lock.
            if self._model is not None:
                return self._model
            cached = _MODEL_CACHE.get(self._model_name)
            if cached is not None:
                self._model = cached
                return self._model

            try:
                from sentence_transformers import SentenceTransformer
                # Check environment variable, default to False to prevent RCE
                _rce_val = os.environ.get("CRG_ALLOW_REMOTE_CODE", "0")
                allow_remote_code = _rce_val.lower() in ("1", "true", "yes")

                model = SentenceTransformer(
                    self._model_name,
                    trust_remote_code=allow_remote_code,
                )
            except ImportError:
                raise ImportError(
                    "sentence-transformers not installed. "
                    "Run: pip install code-review-graph[embeddings]"
                )

            # Publish only a fully constructed model. Failed attempts leave
            # both the provider and shared cache empty so a waiter can retry.
            _MODEL_CACHE[self._model_name] = model
            self._model = model
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(texts, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        model = self._get_model()
        # Use the model's own query prompt when its config ships one.
        prompts = getattr(model, "prompts", None)
        if isinstance(prompts, dict) and prompts.get("query"):
            vector = model.encode([text], prompt_name="query", show_progress_bar=False)[0]
            return vector.tolist()
        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        model = self._get_model()
        if hasattr(model, "get_embedding_dimension"):
            return model.get_embedding_dimension()
        return model.get_sentence_embedding_dimension()

    @property
    def name(self) -> str:
        return f"local:{self._model_name}"


class GoogleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, api_key: str, model: str = "gemini-embedding-001") -> None:
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            self.model = model
            self._dimension: int | None = None
        except ImportError:
            raise ImportError(
                "google-genai not installed. "
                "Run: pip install \"code-review-graph[google-embeddings]\""
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = 100
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = self._call_with_retry(
                lambda b=batch: self._client.models.embed_content(
                    model=self.model,
                    contents=b,
                    config={"task_type": "RETRIEVAL_DOCUMENT"},
                )
            )
            results.extend([e.values for e in response.embeddings])
        if self._dimension is None and results:
            self._dimension = len(results[0])
        return results

    @staticmethod
    def _call_with_retry(fn, max_retries: int = 3):
        """Call fn with exponential backoff on transient API errors."""
        retryable_statuses = ("429", "500", "503")
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                # Retry on rate-limit (429) or server errors (5xx)
                err_str = str(e)
                is_retryable = any(status in err_str for status in retryable_statuses)
                if not is_retryable:
                    logger.debug(
                        "Non-retryable Gemini API error: %s",
                        type(e).__name__,
                    )
                    raise
                if attempt == max_retries - 1:
                    logger.error(
                        "Gemini API request failed after %d requests.",
                        max_retries,
                    )
                    raise

                wait = 2 ** attempt

                logger.warning(
                    "Gemini API retry %d/%d in %ds (%s): %s",
                    attempt + 1,
                    max_retries,
                    wait,
                    type(e).__name__,
                    e,
                )

                time.sleep(wait)

    def embed_query(self, text: str) -> list[float]:
        # One string is one content, hence one embedding.
        contents: ContentListUnion = text
        response = self._call_with_retry(
            lambda: self._client.models.embed_content(
                model=self.model,
                contents=contents,
                config={"task_type": "RETRIEVAL_QUERY"},
            )
        )
        vec = response.embeddings[0].values
        if self._dimension is None:
            self._dimension = len(vec)
        return vec

    @property
    def dimension(self) -> int:
        if self._dimension is not None:
            return self._dimension
        # Default for gemini-embedding-001; updated dynamically after first call
        return 768

    @property
    def name(self) -> str:
        return f"google:{self.model}"


class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """MiniMax embo-01 embedding provider (1536 dimensions).

    Uses the MiniMax Embeddings API (https://api.minimax.io/v1/embeddings)
    with the embo-01 model. Requires the MINIMAX_API_KEY environment variable.
    """

    _ENDPOINT = "https://api.minimax.io/v1/embeddings"
    _MODEL = "embo-01"
    _DIMENSION = 1536

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _call_api(self, texts: list[str], task_type: str) -> list[list[float]]:
        import json as _json
        import urllib.request

        payload = _json.dumps({
            "model": self._MODEL,
            "texts": texts,
            "type": task_type,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                import ssl
                _ssl_ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx) as resp:  # nosec B310
                    body = _json.loads(resp.read().decode("utf-8"))

                base_resp = body.get("base_resp", {})
                if base_resp.get("status_code", 0) != 0:
                    raise RuntimeError(
                        f"MiniMax API error: {base_resp.get('status_msg', 'unknown')}"
                    )

                return body["vectors"]
            except Exception as e:
                err_str = str(e)
                is_retryable = "429" in err_str or "500" in err_str or "503" in err_str
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "MiniMax API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

        return []  # unreachable, but keeps mypy happy

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = 100
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            results.extend(self._call_api(batch, "db"))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._call_api([text], "query")[0]

    @property
    def dimension(self) -> int:
        return self._DIMENSION

    @property
    def name(self) -> str:
        return f"minimax:{self._MODEL}"


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider.

    Works with any endpoint that speaks the OpenAI ``/v1/embeddings`` schema:
    - Real OpenAI API (``https://api.openai.com/v1``)
    - Azure OpenAI
    - Self-hosted gateways: new-api, LiteLLM, vLLM, LocalAI, Ollama (openai mode)

    Provider identity in ``name`` includes both the model and the endpoint
    host (``openai:{model}@{host}``), so switching base URL while keeping the
    same model ID re-partitions the embeddings table and forces a clean
    re-embed. This is the only defense against silently mixing vector spaces
    from different backends (e.g. real OpenAI vs. an OpenAI-compatible
    gateway that ships different weights under the same model name).

    When no dimension is explicitly requested, it is detected from the first
    response and retained as local metadata. Switching the ``model`` in the
    environment also changes ``provider.name`` and triggers re-embed via the
    same isolation key.
    """

    _DEFAULT_BATCH_SIZE = 100

    # Default ports by scheme; stripped from the host_key so the user can't
    # accidentally force a re-embed by toggling an explicit default port.
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int | None = None,
        timeout: int = 120,
        batch_size: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._requested_dimension = dimension
        self._dimension = dimension
        self._timeout = timeout
        self._batch_size = batch_size or self._DEFAULT_BATCH_SIZE
        self._host_key = self._make_host_key(self._base_url)

    @classmethod
    def _make_host_key(cls, base_url: str) -> str:
        """Normalize the identity key used in ``provider.name``.

        Codex review pushed this well past naive ``netloc`` because that
        alone has three leaks:

        1. ``netloc`` preserves ``userinfo`` (``user:pass@host``) — we'd
           persist credentials into the DB's ``embeddings.provider`` column.
           Use ``hostname`` instead.
        2. Default ports (``:80`` for http, ``:443`` for https) are
           semantically identical to omitting the port; keeping them would
           cause spurious re-embeds when the user just spelled the URL
           differently.
        3. Path is part of the backend identity for path-routed gateways:
           ``https://gw/openai/v1`` and ``https://gw/vendor-b/v1`` front
           different models and must not share cached vectors.
        """
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()
        port = parsed.port
        if port and port != cls._DEFAULT_PORTS.get(scheme):
            # Bracket IPv6 literals when appending a port.
            host_part = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
        else:
            host_part = hostname
        # Preserve path routing. Trim any trailing slash and any
        # ``/embeddings`` suffix that callers may have included — we append
        # that ourselves when building the request URL.
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/embeddings"):
            path = path[: -len("/embeddings")].rstrip("/")
        # Include scheme: http and https to the same host+path front
        # different endpoints in practice (plaintext vs TLS, dev vs prod
        # gateway), and sharing cached vectors across them is the same
        # silent-mixing failure mode as switching base URL entirely.
        return f"{scheme}://{host_part}{path}" if path else f"{scheme}://{host_part}"

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        import http.client
        import json as _json
        import socket
        import ssl
        import urllib.error
        import urllib.request

        body: dict[str, Any] = {"model": self._model, "input": texts}
        # Forward only a dimension explicitly requested by the user. The
        # model name may be an Azure deployment or gateway alias, so it cannot
        # tell us whether the endpoint accepts dimension reduction. A dimension
        # learned from a response is local metadata and must never leak into a
        # later request.
        if self._requested_dimension is not None:
            body["dimensions"] = self._requested_dimension

        payload = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                _ssl_ctx = ssl.create_default_context()
                try:
                    with urllib.request.urlopen(  # nosec B310
                        req, timeout=self._timeout, context=_ssl_ctx,
                    ) as resp:
                        raw = resp.read().decode("utf-8")
                except urllib.error.HTTPError as http_err:
                    # 429 / 5xx: re-raise and let the outer retry loop handle it.
                    # (We must not convert to RuntimeError here or retry below
                    # can't tell it was a transient HTTP failure.)
                    if http_err.code == 429 or 500 <= http_err.code < 600:
                        raise
                    # Other 4xx: surface the API error body instead of a bare
                    # "400 Bad Request" — gateways like new-api return JSON
                    # with the real reason (batch size limits, invalid model,
                    # etc.) which is far more actionable.
                    try:
                        err_body = http_err.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = ""
                    err_msg = err_body or str(http_err)
                    try:
                        parsed = _json.loads(err_body)
                        if isinstance(parsed, dict) and "error" in parsed:
                            err_obj = parsed["error"]
                            err_msg = (
                                err_obj.get("message", err_msg)
                                if isinstance(err_obj, dict) else str(err_obj)
                            )
                    except Exception:  # nosec B110
                        # Non-JSON error body is fine: we already seeded
                        # err_msg with the raw body above, so fall through.
                        pass
                    raise RuntimeError(
                        f"OpenAI API HTTP {http_err.code}: {err_msg}"
                    ) from http_err

                response = _json.loads(raw)

                if "error" in response:
                    err = response["error"]
                    msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"OpenAI API error: {msg}")

                data = response.get("data", [])
                if not data:
                    raise RuntimeError("OpenAI API returned empty data")
                # OpenAI spec: data[i].index maps to input[i], but some
                # compatible gateways re-order results or drop entries on
                # partial failure, and others omit `index` entirely. Three
                # disjoint cases:
                #   1. All items have a valid int ``index``: must form a
                #      permutation of 0..N-1, then sort and use.
                #   2. NO item carries an ``index`` field: trust server
                #      order, only verify count matches.
                #   3. Anything in between (partial indices, str indices,
                #      missing on some): refuse. Zipping server order in
                #      that case would happily misalign the indexed items.
                any_has_index = any("index" in item for item in data)
                all_int_index = all(
                    isinstance(item.get("index"), int) for item in data
                )
                if all_int_index:
                    expected = set(range(len(texts)))
                    indices = [int(item["index"]) for item in data]
                    if len(set(indices)) != len(indices) or set(indices) != expected:
                        raise RuntimeError(
                            "OpenAI API returned malformed indices "
                            f"(got {indices}, expected permutation of "
                            f"0..{len(texts) - 1}) — refusing to misalign vectors."
                        )
                    data = sorted(data, key=lambda item: int(item["index"]))
                elif not any_has_index:
                    if len(data) != len(texts):
                        raise RuntimeError(
                            f"OpenAI API returned {len(data)} embeddings for "
                            f"{len(texts)} inputs with no index field — "
                            "refusing to misalign vectors."
                        )
                else:
                    # Mixed: some items have index, others don't (or carry
                    # non-int index). Server order would silently misplace
                    # the indexed items, so we refuse.
                    raise RuntimeError(
                        "OpenAI API returned mixed indexed/unindexed data — "
                        "refusing to misalign vectors."
                    )

                vectors = [item["embedding"] for item in data]
                if vectors and self._dimension is None:
                    self._dimension = len(vectors[0])
                return vectors

            except Exception as e:
                # Retryable = HTTP 429/5xx, network/timeout/TLS issues.
                # Non-retryable = HTTP 4xx (other), malformed responses,
                # misaligned data length — those are caller-side bugs that
                # will keep failing on retry.
                is_retryable = False
                if isinstance(e, urllib.error.HTTPError):
                    is_retryable = e.code == 429 or 500 <= e.code < 600
                elif isinstance(e, (
                    urllib.error.URLError,
                    socket.timeout,
                    TimeoutError,
                    ConnectionError,
                    ssl.SSLError,
                    # Reverse proxies and edge gateways surface transient
                    # disconnects as these stdlib classes. Real incidents
                    # have been observed on Cloudflare-fronted endpoints
                    # and on LiteLLM when upstream providers hiccup.
                    http.client.IncompleteRead,
                    http.client.BadStatusLine,
                    http.client.RemoteDisconnected,
                )):
                    is_retryable = True
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "OpenAI embeddings API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

        return []  # unreachable

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            results.extend(self._call_api(texts[i:i + self._batch_size]))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._call_api([text])[0]

    @property
    def dimension(self) -> int:
        if self._dimension is not None:
            return self._dimension
        # Default for text-embedding-3-small; updated after first call.
        return 1536

    @property
    def name(self) -> str:
        # Endpoint-aware identity: model alone is NOT enough — two backends
        # can serve the same model ID with different weights or dimensions,
        # and re-using cached embeddings across them silently corrupts
        # semantic ranking. Including the host partitions the embeddings
        # table so switching CRG_OPENAI_BASE_URL triggers a safe re-embed.
        return f"openai:{self._model}@{self._host_key}"


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embedding provider.

    Uses Voyage's embeddings API with document/query input types so indexed
    source-derived node text and search queries are embedded with the task hint
    Voyage expects. Provider identity includes model, dimension, dtype, and
    endpoint to avoid mixing incompatible vector spaces.
    """

    _DEFAULT_BASE_URL = "https://api.voyageai.com/v1"
    _DEFAULT_MODEL = "voyage-code-3"
    _DEFAULT_DIMENSION = 1024
    _DEFAULT_OUTPUT_DTYPE = "float"
    _DEFAULT_BATCH_SIZE = 100

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        output_dimension: int | None = None,
        output_dtype: str | None = None,
        timeout: int = 120,
        batch_size: int | None = None,
        min_interval_sec: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or self._DEFAULT_BASE_URL).rstrip("/")
        self._model = model or self._DEFAULT_MODEL
        self._output_dimension = output_dimension or self._DEFAULT_DIMENSION
        self._output_dtype = output_dtype or self._DEFAULT_OUTPUT_DTYPE
        self._timeout = timeout
        self._batch_size = batch_size or self._DEFAULT_BATCH_SIZE
        self._min_interval_sec = max(0.0, min_interval_sec)
        self._last_request_at = 0.0
        self._host_key = OpenAIEmbeddingProvider._make_host_key(self._base_url)

    def _wait_for_rate_limit_slot(self) -> None:
        if self._min_interval_sec <= 0:
            return
        now = time.monotonic()
        if self._last_request_at > 0:
            wait = self._min_interval_sec - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self._last_request_at = now

    def _call_api(self, texts: list[str], input_type: str) -> list[list[float]]:
        import http.client
        import json as _json
        import socket
        import ssl
        import urllib.error
        import urllib.request

        body: dict[str, Any] = {
            "model": self._model,
            "input": texts,
            "input_type": input_type,
            "output_dimension": self._output_dimension,
            "output_dtype": self._output_dtype,
        }

        payload = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                _ssl_ctx = ssl.create_default_context()
                try:
                    self._wait_for_rate_limit_slot()
                    with urllib.request.urlopen(  # nosec B310
                        req, timeout=self._timeout, context=_ssl_ctx,
                    ) as resp:
                        raw = resp.read().decode("utf-8")
                except urllib.error.HTTPError as http_err:
                    if http_err.code == 429 or 500 <= http_err.code < 600:
                        raise
                    try:
                        err_body = http_err.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = ""
                    err_msg = err_body or str(http_err)
                    try:
                        parsed = _json.loads(err_body)
                        if isinstance(parsed, dict) and "error" in parsed:
                            err_obj = parsed["error"]
                            err_msg = (
                                err_obj.get("message", err_msg)
                                if isinstance(err_obj, dict) else str(err_obj)
                            )
                    except Exception:  # nosec B110
                        pass
                    raise RuntimeError(
                        f"Voyage API HTTP {http_err.code}: {err_msg}"
                    ) from http_err

                response = _json.loads(raw)

                if "error" in response:
                    err = response["error"]
                    msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Voyage API error: {msg}")

                data = response.get("data", [])
                if not data:
                    raise RuntimeError("Voyage API returned empty data")

                any_has_index = any("index" in item for item in data)
                all_int_index = all(
                    isinstance(item.get("index"), int) for item in data
                )
                if all_int_index:
                    expected = set(range(len(texts)))
                    indices = [int(item["index"]) for item in data]
                    if len(set(indices)) != len(indices) or set(indices) != expected:
                        raise RuntimeError(
                            "Voyage API returned malformed indices "
                            f"(got {indices}, expected permutation of "
                            f"0..{len(texts) - 1}) — refusing to misalign vectors."
                        )
                    data = sorted(data, key=lambda item: int(item["index"]))
                elif not any_has_index:
                    if len(data) != len(texts):
                        raise RuntimeError(
                            f"Voyage API returned {len(data)} embeddings for "
                            f"{len(texts)} inputs with no index field — "
                            "refusing to misalign vectors."
                        )
                else:
                    raise RuntimeError(
                        "Voyage API returned mixed indexed/unindexed data — "
                        "refusing to misalign vectors."
                    )

                return [item["embedding"] for item in data]

            except Exception as e:
                is_retryable = False
                if isinstance(e, urllib.error.HTTPError):
                    is_retryable = e.code == 429 or 500 <= e.code < 600
                elif isinstance(e, (
                    urllib.error.URLError,
                    socket.timeout,
                    TimeoutError,
                    ConnectionError,
                    ssl.SSLError,
                    http.client.IncompleteRead,
                    http.client.BadStatusLine,
                    http.client.RemoteDisconnected,
                )):
                    is_retryable = True
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "Voyage embeddings API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

        return []  # unreachable

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            results.extend(self._call_api(texts[i:i + self._batch_size], "document"))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._call_api([text], "query")[0]

    @property
    def dimension(self) -> int:
        return self._output_dimension

    @property
    def name(self) -> str:
        return (
            f"voyage:{self._model}:dim{self._output_dimension}:"
            f"{self._output_dtype}@{self._host_key}"
        )


CLOUD_PROVIDERS = {"google", "minimax", "openai", "voyage"}


def _is_localhost_url(url: str) -> bool:
    """Return True if url points to a localhost host (never treat as cloud egress).

    Uses urlparse.hostname so we compare the actual host, not a substring
    match that could be fooled by e.g. ``https://my-openai.127.0.0.1.nip.io``.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    # nosec B104: we're *matching* a URL hostname, not binding a listener.
    return host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}  # nosec B104


def _warn_cloud_egress(provider_name: str) -> None:
    """Print a stderr warning before a cloud embedding provider is used.

    The warning is suppressed when ``CRG_ACCEPT_CLOUD_EMBEDDINGS=1`` is
    set in the environment, so scripted / CI workloads can acknowledge
    once and move on. Use stderr (never stdin/input) to stay compatible
    with the MCP stdio transport — anything we write to stdout would
    corrupt the JSON-RPC stream. See: #174
    """
    if os.environ.get("CRG_ACCEPT_CLOUD_EMBEDDINGS", "").strip() == "1":
        return
    print(
        f"\n⚠️  code-review-graph: about to embed code via the '{provider_name}' "
        "cloud provider.\n"
        "    Your source code (function names, docstrings, file paths) will be "
        "sent to an external API.\n"
        "    This is necessary for semantic search with the cloud provider you "
        "selected.\n"
        "    To skip this warning in future runs, set "
        "CRG_ACCEPT_CLOUD_EMBEDDINGS=1 in your environment.\n"
        "    To stay fully offline, use the default 'local' provider instead "
        "(no API key needed).\n",
        file=sys.stderr,
    )


_VALID_PROVIDERS = {"local", "openai", "google", "minimax", "voyage"}


def get_provider(
    provider: str | None = None,
    model: str | None = None,
) -> EmbeddingProvider | None:
    """Get an embedding provider by name.

    Args:
        provider: Provider name. One of "local", "google", "minimax",
                  "openai", "voyage", or None. When omitted, configured
                  OpenAI-compatible credentials select OpenAI; otherwise the
                  local provider is used. Names are case-insensitive and
                  surrounding whitespace is ignored; unknown names raise
                  ValueError instead of silently falling back to the local
                  provider. Google requires GOOGLE_API_KEY env var and explicit
                  opt-in. MiniMax requires MINIMAX_API_KEY env var and explicit
                  opt-in. Voyage requires VOYAGE_API_KEY. OpenAI requires
                  CRG_OPENAI_API_KEY + CRG_OPENAI_BASE_URL + CRG_OPENAI_MODEL
                  env vars (or the ``model`` arg). The egress warning is
                  skipped when the base URL points to localhost.
                  Cloud providers emit a one-time stderr warning before use
                  unless ``CRG_ACCEPT_CLOUD_EMBEDDINGS=1`` is set. See: #174
        model: Model name/path to use. For local provider this is any
               sentence-transformers compatible model. Falls back to
               CRG_EMBEDDING_MODEL env var, then to all-MiniLM-L6-v2.
               For Google provider this is a Gemini model ID.
               For OpenAI provider this overrides CRG_OPENAI_MODEL.
               For Voyage provider this overrides CRG_VOYAGE_MODEL.

    Raises:
        ValueError: If the provider name is not one of the known providers,
                    or if required environment variables are missing.
    """
    name = provider.strip().lower() if provider else ""
    if name and name not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown embedding provider '{name}'. "
            "Valid: local, openai, google, minimax, voyage"
        )

    # When no explicit provider is given but OpenAI-compatible env vars are
    # configured, default to the openai provider so MCP tool calls that omit
    # the optional `provider` parameter still use the configured backend
    # (#551).
    if (
        provider is None
        and os.environ.get("CRG_OPENAI_API_KEY")
        and os.environ.get("CRG_OPENAI_BASE_URL")
    ):
        name = "openai"

    if name == "openai":
        api_key = os.environ.get("CRG_OPENAI_API_KEY")
        base_url = os.environ.get("CRG_OPENAI_BASE_URL")
        resolved_model = model or os.environ.get("CRG_OPENAI_MODEL")
        if not api_key or not base_url or not resolved_model:
            missing = [
                name for name, val in [
                    ("CRG_OPENAI_API_KEY", api_key),
                    ("CRG_OPENAI_BASE_URL", base_url),
                    ("CRG_OPENAI_MODEL", resolved_model),
                ] if not val
            ]
            raise ValueError(
                "Missing required environment variable(s) for the OpenAI "
                f"embedding provider: {', '.join(missing)}."
            )
        dim_env = os.environ.get("CRG_OPENAI_DIMENSION")
        dimension = int(dim_env) if dim_env else None
        batch_env = os.environ.get("CRG_OPENAI_BATCH_SIZE")
        batch_size = int(batch_env) if batch_env else None
        if not _is_localhost_url(base_url):
            _warn_cloud_egress("openai")
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            base_url=base_url,
            model=resolved_model,
            dimension=dimension,
            batch_size=batch_size,
        )

    if name == "minimax":
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise ValueError(
                "MINIMAX_API_KEY environment variable is required for "
                "the MiniMax embedding provider."
            )
        _warn_cloud_egress("minimax")
        return MiniMaxEmbeddingProvider(api_key=api_key)

    if name == "voyage":
        api_key = os.environ.get("VOYAGE_API_KEY")
        if not api_key:
            raise ValueError(
                "VOYAGE_API_KEY environment variable is required for "
                "the Voyage embedding provider."
            )
        base_url = (
            os.environ.get("CRG_VOYAGE_BASE_URL")
            or VoyageEmbeddingProvider._DEFAULT_BASE_URL
        )
        resolved_model = (
            model
            or os.environ.get("CRG_VOYAGE_MODEL")
            or VoyageEmbeddingProvider._DEFAULT_MODEL
        )
        dim_env = os.environ.get("CRG_VOYAGE_OUTPUT_DIMENSION")
        output_dimension = int(dim_env) if dim_env else VoyageEmbeddingProvider._DEFAULT_DIMENSION
        output_dtype = (
            os.environ.get("CRG_VOYAGE_OUTPUT_DTYPE")
            or VoyageEmbeddingProvider._DEFAULT_OUTPUT_DTYPE
        )
        batch_env = os.environ.get("CRG_VOYAGE_BATCH_SIZE")
        batch_size = int(batch_env) if batch_env else None
        min_interval_env = os.environ.get("CRG_VOYAGE_MIN_INTERVAL_SEC")
        min_interval_sec = float(min_interval_env) if min_interval_env else 0.0
        if not _is_localhost_url(base_url):
            _warn_cloud_egress("voyage")
        return VoyageEmbeddingProvider(
            api_key=api_key,
            base_url=base_url,
            model=resolved_model,
            output_dimension=output_dimension,
            output_dtype=output_dtype,
            batch_size=batch_size,
            min_interval_sec=min_interval_sec,
        )

    if name == "google":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY environment variable is required for "
                "the Google embedding provider."
            )
        _warn_cloud_egress("google")
        try:
            return GoogleEmbeddingProvider(
                api_key=api_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            return None

    # Default: local
    if not _check_available():
        return None
    try:
        return LocalEmbeddingProvider(model_name=model)
    except ImportError:
        return None


def _check_available() -> bool:
    """Check whether local embedding support is available."""
    # A model load holds the lock for as long as a download takes; answer
    # from the import system (no import, so no Torch init) instead of
    # queueing searches behind it.
    if not _MODEL_INIT_LOCK.acquire(blocking=False):
        from .embedding_providers.profiles import module_available

        return module_available("sentence_transformers")
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False
    finally:
        _MODEL_INIT_LOCK.release()


# ---------------------------------------------------------------------------
# SQLite vector storage
# ---------------------------------------------------------------------------

_EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider ON embeddings(provider);
"""


def _encode_vector(vec: list[float]) -> bytes:
    """Encode a float vector as a compact binary blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def _decode_vector(blob: bytes) -> list[float]:
    """Decode a binary blob back to a float vector."""
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


_STORED_DTYPES = {"float16": "e", "float32": "f"}
_SQL_CHUNK = 500
_MATMUL_CHUNK = 16384
_MATRIX_CACHE_MAX = 4
# (db path, provider id, dim) -> (change token, names, row matrix, row norms)
_matrix_cache: collections.OrderedDict[
    tuple[str, str, int], tuple[Any, list[str], Any, Any]
] = collections.OrderedDict()
_local_generation: dict[str, int] = {}
_matrix_lock = threading.Lock()


def _encode_stored_vector(vec: Any, dtype: str = "float16") -> bytes:
    """L2-normalize *vec* and pack it as float16 (default) or float32."""
    values = [float(x) for x in vec]
    norm = sum(x * x for x in values) ** 0.5
    if norm > 0:
        values = [x / norm for x in values]
    return struct.pack(f"{len(values)}{_STORED_DTYPES[dtype]}", *values)


def _decode_stored(blob: bytes, dim: int) -> Optional[list[float]]:
    """Decode a *dim*-component row stored as float16 or float32 (None: other dim)."""
    if dim <= 0:
        return None
    if len(blob) == 2 * dim:
        return list(struct.unpack(f"{dim}e", blob))
    if len(blob) == 4 * dim:
        return list(struct.unpack(f"{dim}f", blob))
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    # NaN fails every comparison, so test the norms positively and check they
    # are finite: a stored non-finite row scores 0.0 rather than sorting to
    # the top of the ranking as NaN.
    if not (norm_a > 0 and norm_b > 0):
        return 0.0
    if not (math.isfinite(norm_a) and math.isfinite(norm_b)):
        return 0.0
    return dot / (norm_a * norm_b)


_IDENTIFIER_SPLIT_RE = re.compile(r"([a-z])([A-Z])|[_./\-]+")
_MAX_EMBEDDED_DOCSTRING_CHARS = 400


def _split_identifier(name: str) -> str:
    """Split snake_case / camelCase / PascalCase / dotted into space-separated words.

    Examples:
        get_route_handler -> "get route handler"
        APIRoute          -> "API Route"
        dispatch_request  -> "dispatch request"
        full_dispatch_request -> "full dispatch request"
    """
    if not name:
        return ""
    # Insert space between lowercase->uppercase transitions, then collapse
    # snake_case / dotted / hyphenated separators.
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"[_./\-]+", " ", spaced)
    return " ".join(spaced.split())


def _node_to_text(node: GraphNode) -> str:
    """Convert a node to a searchable text representation.

    Designed so natural-language queries land on the right node, not just on
    the enclosing class. We include the dotted ``Parent.name`` form, the
    identifier split into words, an explicit ``"in <Parent>"`` phrase, the
    enclosing module directory, and the language. Tested by the
    ``multi_hop_retrieval`` benchmark — see ``docs/REPRODUCING.md``.
    """
    parts: list[str] = []

    # 1. Dotted form first — strongest lexical signal for "method in class"
    if node.parent_name and node.kind != "File":
        parts.append(f"{node.parent_name}.{node.name}")

    # 2. Bare name (always present)
    parts.append(node.name)

    # 3. Split-words form of the name (only if it differs from the bare name)
    name_split = _split_identifier(node.name)
    if name_split and name_split.lower() != node.name.lower():
        parts.append(name_split)

    # 4. Kind ("function", "class", "test", ...)
    if node.kind != "File":
        parts.append(node.kind.lower())

    # 5. Parent context with the split form too
    if node.parent_name:
        parts.append(f"in {node.parent_name}")
        parent_split = _split_identifier(node.parent_name)
        if parent_split and parent_split.lower() != node.parent_name.lower():
            parts.append(parent_split)

    # 6. Signature bits
    if node.params:
        parts.append(node.params)
    if node.return_type:
        parts.append(f"returns {node.return_type}")

    # 7. Documentation summary.  Existing databases may contain arbitrary
    # values in ``extra`` so accept strings only, normalize whitespace, and
    # re-apply the parser's bound before the text enters a provider request.
    raw_docstring = node.extra.get("docstring") if node.extra else None
    if isinstance(raw_docstring, str):
        docstring = " ".join(raw_docstring.split())[:_MAX_EMBEDDED_DOCSTRING_CHARS]
        if docstring:
            parts.append(docstring)

    # 8. Module / directory context from the file path — gives queries a
    # term like "routing" or "client" to anchor against.
    if node.file_path:
        parent_dir = Path(node.file_path).parent.name
        if parent_dir and parent_dir not in (".", "src", "lib"):
            parts.append(parent_dir)

    # 9. Language
    if node.language:
        parts.append(node.language)

    return " ".join(parts)


class EmbeddingStore:
    """Manages vector embeddings for graph nodes in SQLite.

    Rows are written L2-normalized as float16 (``dtype="float32"`` keeps full
    precision); float32 rows from older releases are still read. A row's
    dtype follows from its byte length and the provider's dimension, so the
    table needs no schema change.
    """

    def __init__(
        self,
        db_path: str | Path,
        provider: str | None = None,
        model: str | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        dtype: str | None = None,
    ) -> None:
        """Open the vector table of *db_path*.

        The provider is *embedding_provider* when given, else the named
        *provider*/*model*. With neither, enabled repository settings pick
        the profile provider; otherwise the historic default applies.
        """
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False,
            isolation_level=None,
        )
        settings: EmbeddingSettings | None = None
        if embedding_provider is not None:
            self.provider: EmbeddingProvider | None = embedding_provider
        elif provider is None and model is None and (
            settings := load_embedding_settings(repo_root_for_db(self.db_path))
        ).enabled:
            parity = mlx_parity_for(self._conn, _mlx_model(settings))
            self.provider, _ = provider_for_settings(settings, mlx_parity=parity)
        else:
            self.provider = get_provider(provider, model=model)
        self.available = self.provider is not None
        dtype = dtype or (settings.dtype if settings is not None else "float16")
        if dtype not in _STORED_DTYPES:
            self._conn.close()
            raise ValueError(f"Unsupported embedding dtype {dtype!r}")
        self.dtype = dtype
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_EMBEDDINGS_SCHEMA)

        # Migration for existing DBs missing the provider column
        try:
            self._conn.execute("SELECT provider FROM embeddings LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute(
                "ALTER TABLE embeddings ADD COLUMN provider "
                "TEXT NOT NULL DEFAULT 'unknown'"
            )

        self._conn.commit()

    def __enter__(self) -> "EmbeddingStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- writes ------------------------------------------------------------

    def _existing(self, qualified_names: list[str]) -> dict[str, tuple[str, str]]:
        """``qualified_name -> (text_hash, provider)`` in chunked IN queries."""
        found: dict[str, tuple[str, str]] = {}
        for i in range(0, len(qualified_names), _SQL_CHUNK):
            chunk = qualified_names[i:i + _SQL_CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                "SELECT qualified_name, text_hash, provider FROM embeddings "  # nosec B608
                f"WHERE qualified_name IN ({marks})",
                chunk,
            ):
                found[row["qualified_name"]] = (row["text_hash"], row["provider"])
        return found

    def _embed_texts(self, texts: list[str]) -> list[Any]:
        provider = self.provider
        assert provider is not None
        as_array = getattr(provider, "embed_documents_array", None)
        if callable(as_array):
            vectors = list(as_array(texts))
        elif hasattr(provider, "embed_documents"):
            vectors = provider.embed_documents(texts)
        else:
            vectors = provider.embed(texts)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embedding provider {provider.name} returned {len(vectors)} vectors "
                f"for {len(texts)} texts",
            )
        return vectors

    def embed_nodes(self, nodes: list[GraphNode], batch_size: int = 64) -> int:
        """Compute and store embeddings for nodes whose text or provider changed.

        Batches are length-sorted (less padding for transformer backends) and
        each batch is one transaction, so a failure keeps completed batches.
        """
        if not self.provider:
            return 0
        provider_name = self.provider.name

        candidates: dict[str, tuple[GraphNode, str, str]] = {}
        for node in nodes:
            if node.kind == "File":
                continue
            text = _node_to_text(node)
            candidates[node.qualified_name] = (
                node, text, hashlib.sha256(text.encode()).hexdigest(),
            )
        existing = self._existing(list(candidates))
        to_embed = [
            item for qn, item in candidates.items()
            if existing.get(qn) != (item[2], provider_name)
        ]
        if not to_embed:
            return 0
        to_embed.sort(key=lambda item: len(item[1]))

        embedded = 0
        try:
            for i in range(0, len(to_embed), batch_size):
                batch = to_embed[i:i + batch_size]
                vectors = self._embed_texts([text for _, text, _ in batch])
                rows = [
                    (node.qualified_name, _encode_stored_vector(vec, self.dtype),
                     text_hash, provider_name)
                    for (node, _text, text_hash), vec in zip(batch, vectors)
                ]
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO embeddings "
                        "(qualified_name, vector, text_hash, provider) VALUES (?, ?, ?, ?)",
                        rows,
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                embedded += len(rows)
        finally:
            if embedded:
                self._bump_generation()
        return embedded

    def _bump_generation(self) -> None:
        """Invalidate search matrices built from this database."""
        key = _db_key(self.db_path)
        with _matrix_lock:
            _local_generation[key] = _local_generation.get(key, 0) + 1
        if _has_table(self._conn, "metadata"):
            try:
                self._conn.execute(
                    "INSERT INTO metadata (key, value) VALUES ('embeddings_generation', '1') "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
                )
            except sqlite3.OperationalError as exc:
                logger.warning("Cannot bump embeddings_generation: %s", exc)

    def remove_node(self, qualified_name: str) -> None:
        self._conn.execute(
            "DELETE FROM embeddings WHERE qualified_name = ?", (qualified_name,)
        )
        self._conn.commit()
        self._bump_generation()

    def remove_provider(self, provider_name: str | None = None) -> int:
        """Delete every vector (or only one provider's); returns the row count."""
        if provider_name is None:
            cursor = self._conn.execute("DELETE FROM embeddings")
        else:
            cursor = self._conn.execute(
                "DELETE FROM embeddings WHERE provider = ?", (provider_name,),
            )
        self._bump_generation()
        return max(cursor.rowcount, 0)

    def purge_orphans(self) -> int:
        """Delete vectors whose graph node no longer exists.

        Embeddings and graph nodes normally share a SQLite file.  Standalone
        embedding databases remain supported, so a missing ``nodes`` table is
        an intentional no-op rather than an error.
        """
        if not _has_table(self._conn, "nodes"):
            return 0
        cursor = self._conn.execute(
            "DELETE FROM embeddings "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM nodes "
            "WHERE nodes.qualified_name = embeddings.qualified_name"
            ")",
        )
        self._conn.commit()
        purged = max(cursor.rowcount, 0)
        if purged:
            self._bump_generation()
        return purged

    # -- reads -------------------------------------------------------------

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def count_for_provider(self, provider_name: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE provider = ?", (provider_name,),
        ).fetchone()[0]

    def stale_count(self, provider_name: str) -> int:
        """Embeddable nodes with no vector under *provider_name* (0 without a nodes table)."""
        if not _has_table(self._conn, "nodes"):
            return 0
        return self._conn.execute(
            "SELECT COUNT(*) FROM nodes n WHERE n.kind != 'File' AND NOT EXISTS ("
            "SELECT 1 FROM embeddings e WHERE e.qualified_name = n.qualified_name "
            "AND e.provider = ?)",
            (provider_name,),
        ).fetchone()[0]

    def disk_bytes(self, provider_name: str | None = None) -> int:
        """Bytes of stored vector data (all providers, or one)."""
        if provider_name is None:
            row = self._conn.execute("SELECT SUM(LENGTH(vector)) FROM embeddings").fetchone()
        else:
            row = self._conn.execute(
                "SELECT SUM(LENGTH(vector)) FROM embeddings WHERE provider = ?",
                (provider_name,),
            ).fetchone()
        return int(row[0] or 0)

    def search(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        """Search for nodes by semantic similarity.

        Uses a vectorised (numpy) ranking pass when numpy is importable,
        falling back to the pure-Python loop otherwise. Both paths produce
        the same ranking (up to float tolerance). A zero-norm query vector
        returns an empty list rather than a list of meaningless ties; a
        stored zero-norm row always scores 0.0 and never wins the ranking.
        Only rows of the current provider and the query's dimension take
        part, so a partial re-embed can never mix vector spaces.
        """
        if not self.provider:
            return []
        # A non-positive limit can never return rows; bail out before touching
        # the database or loading an embedding model for the query.
        if limit <= 0:
            return []

        provider_name = self.provider.name
        # Checked before embedding the query so an empty index never loads a model.
        if self._conn.execute(
            "SELECT 1 FROM embeddings WHERE provider = ? LIMIT 1", (provider_name,),
        ).fetchone() is None:
            return []
        query_vec = list(self.provider.embed_query(query))
        query_norm = sum(x * x for x in query_vec) ** 0.5
        # NaN fails every comparison, so test it positively: a provider that
        # emits a non-finite vector must not rank the whole index.
        if not (query_norm > 0.0) or not math.isfinite(query_norm):
            return []

        try:
            import numpy as np
        except ImportError:
            np = None  # numpy is part of the optional [embeddings] extra

        if np is not None:
            return self._search_vectorized(np, query_vec, query_norm, provider_name, limit)
        return self._search_pure_python(query_vec, provider_name, limit)

    def _cache_token(self) -> tuple[Any, ...]:
        generation = None
        if _has_table(self._conn, "metadata"):
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key = 'embeddings_generation'",
            ).fetchone()
            generation = row[0] if row else None
        count, max_rowid = self._conn.execute(
            "SELECT COUNT(*), MAX(rowid) FROM embeddings",
        ).fetchone()
        with _matrix_lock:
            local = _local_generation.get(_db_key(self.db_path), 0)
        return (local, generation, count, max_rowid)

    def _matrix(
        self, np: Any, provider_name: str, dim: int,
    ) -> tuple[list[str], Any, Any]:
        """Rows of one provider and dimension, cached until the table changes.

        Returns ``(names, matrix, norms)``. float16 rows stay float16 in
        memory (half the RAM); any float32 row makes the matrix float32 so
        legacy rows keep full precision. ``norms`` holds each row's real
        length so the caller can divide by it.
        """
        key = (_db_key(self.db_path), provider_name, dim)
        token = self._cache_token()
        with _matrix_lock:
            cached = _matrix_cache.get(key)
            if cached is not None and cached[0] == token:
                _matrix_cache.move_to_end(key)
                return cached[1], cached[2], cached[3]

        names: list[str] = []
        half: list[tuple[int, bytes]] = []
        full: list[tuple[int, bytes]] = []
        skipped = 0
        cursor = self._conn.execute(
            "SELECT qualified_name, vector FROM embeddings WHERE provider = ?",
            (provider_name,),
        )
        while True:
            rows = cursor.fetchmany(20000)
            if not rows:
                break
            for row in rows:
                blob = row["vector"]
                size = len(blob)
                if size == 2 * dim:
                    half.append((len(names), blob))
                elif size == 4 * dim:
                    full.append((len(names), blob))
                else:
                    skipped += 1
                    continue
                names.append(row["qualified_name"])
        if skipped:
            logger.warning(
                "Ignored %d stored vector(s) under %s whose dimension is not %d; "
                "re-embed to include them", skipped, provider_name, dim,
            )

        dtype = np.float32 if full else np.float16
        mat = np.zeros((len(names), dim), dtype=dtype)
        for group, np_dtype in ((half, np.float16), (full, np.float32)):
            if group:
                idx = np.fromiter((i for i, _ in group), dtype=np.int64, count=len(group))
                raw = b"".join(blob for _, blob in group)
                mat[idx] = np.frombuffer(raw, dtype=np_dtype).reshape(len(group), dim)
        # Row norms are kept beside the matrix rather than divided into it. A
        # float16 row is only unit-length to within the format's rounding
        # (~6e-5), and renormalizing in float32 then storing back into float16
        # just reintroduces that error, so the matrix can never be made exactly
        # unit in its own dtype. Dividing the scores instead keeps the memory
        # saving and matches _cosine_similarity, which divides by the row's
        # real norm. Legacy float32 rows were stored unnormalized, so they need
        # this too. Norms are taken in float32 to avoid upcasting the matrix.
        norms = np.empty(len(names), dtype=np.float32)
        for start in range(0, len(names), _MATMUL_CHUNK):
            stop = start + _MATMUL_CHUNK
            block = mat[start:stop].astype(np.float32)
            block_norms = np.linalg.norm(block, axis=1)
            finite = np.isfinite(block_norms)
            # A non-finite row would poison the ranking with NaN; zero it so it
            # scores 0.0 like the scalar path.
            block[~finite] = 0.0
            mat[start:stop] = block
            block_norms[~finite] = 1.0
            norms[start:stop] = block_norms

        with _matrix_lock:
            _matrix_cache[key] = (token, names, mat, norms)
            _matrix_cache.move_to_end(key)
            while len(_matrix_cache) > _MATRIX_CACHE_MAX:
                _matrix_cache.popitem(last=False)
        return names, mat, norms

    def _search_vectorized(
        self, np: Any, query_vec: list[float], query_norm: float,
        provider_name: str, limit: int,
    ) -> list[tuple[str, float]]:
        """Rank stored vectors against ``query_vec`` using numpy.

        ``query_norm`` must already be known non-zero and finite (checked by
        ``search``). A stored zero-norm row scores 0.0 rather than dividing by
        zero, and a non-finite row is zeroed in ``_matrix``.
        """
        q = np.asarray(query_vec, dtype=np.float32) / np.float32(query_norm)
        names, mat, norms = self._matrix(np, provider_name, len(query_vec))
        if not names or limit <= 0:
            return []
        if mat.dtype == np.float32:
            sims = mat @ q
        else:
            # Upcast in slices: float16 matmul has no BLAS path.
            sims = np.empty(len(names), dtype=np.float32)
            for start in range(0, len(names), _MATMUL_CHUNK):
                stop = start + _MATMUL_CHUNK
                sims[start:stop] = mat[start:stop].astype(np.float32) @ q
        # Divide by each row's real norm; a zero norm keeps the row at 0.0.
        np.divide(sims, norms, out=sims, where=norms > 0)
        k = min(limit, len(names))
        top = np.argpartition(-sims, k - 1)[:k] if k < len(names) else np.arange(len(names))
        top = top[np.argsort(-sims[top], kind="stable")]
        return [(names[i], float(sims[i])) for i in top]

    def _search_pure_python(
        self, query_vec: list[float], provider_name: str, limit: int,
    ) -> list[tuple[str, float]]:
        """Rank stored vectors against ``query_vec`` without numpy."""
        dim = len(query_vec)
        scored: list[tuple[str, float]] = []
        cursor = self._conn.execute(
            "SELECT qualified_name, vector FROM embeddings WHERE provider = ?",
            (provider_name,),
        )
        chunk_size = 500
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            for row in rows:
                vec = _decode_stored(row["vector"], dim)
                if vec is None:
                    continue
                scored.append((row["qualified_name"], _cosine_similarity(query_vec, vec)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit] if limit > 0 else []


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None


def _db_key(db_path: str | Path) -> str:
    try:
        return str(Path(db_path).resolve())
    except OSError:
        return str(db_path)


def clear_matrix_cache() -> None:
    """Drop cached search matrices (tests, or to free memory)."""
    with _matrix_lock:
        _matrix_cache.clear()


# ---------------------------------------------------------------------------
# Profiles, state metadata and background embedding
# ---------------------------------------------------------------------------

EMBEDDINGS_STATES = ("off", "ready", "stale", "unavailable")
EMBEDDINGS_META_KEYS = (
    "embeddings_provider",
    "embeddings_model",
    "embeddings_dim",
    "embeddings_state",
    "embeddings_stale_count",
)
MLX_PARITY_KEY = "embeddings_mlx_parity"

_PROFILE_PROVIDERS: dict[str, EmbeddingProvider] = {}
_PROFILE_PROVIDERS_LOCK = threading.Lock()


def repo_root_for_db(db_path: str | Path) -> Path | None:
    """The repository of a graph stored at ``<repo>/.code-review-graph/graph.db``."""
    path = Path(db_path)
    if path.parent.name == ".code-review-graph":
        return path.parent.parent
    return None


def provider_for_settings(
    settings: EmbeddingSettings, *, mlx_parity: bool | None = None,
) -> tuple[EmbeddingProvider | None, Resolution]:
    """Build (or reuse) the provider serving *settings*; never loads a model.

    Returns ``(None, resolution)`` when the profile's backend is missing;
    ``resolution.warning`` says what to install. Local model providers are
    shared process-wide per provider id, so the model loads once.
    """
    resolution = resolve_profile(settings, mlx_parity=mlx_parity)
    if not resolution.available:
        return None, resolution
    if resolution.cloud:
        try:
            provider = get_provider(resolution.profile, model=settings.model)
        except ValueError as exc:
            return None, replace(resolution, available=False, warning=str(exc))
        if provider is None:
            return None, replace(
                resolution, available=False,
                warning=f"embedding provider '{resolution.profile}' is not installed",
            )
        return provider, resolution
    spec = resolution.spec
    assert spec is not None
    if spec.backend == "sentence-transformers":
        return LocalEmbeddingProvider(resolution.model), resolution

    from .embedding_providers.backends import BACKEND_CLASSES

    with _PROFILE_PROVIDERS_LOCK:
        cached = _PROFILE_PROVIDERS.get(resolution.provider_id)
        if cached is None:
            cached = BACKEND_CLASSES[spec.backend](
                spec, resolution.model, resolution.dim,
                threads=settings.effective_threads,
                batch_size=settings.batch_size,
                idle_unload_s=settings.idle_unload_s,
            )
            _PROFILE_PROVIDERS[resolution.provider_id] = cached
    return cached, resolution


def read_embeddings_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The ``embeddings_*`` metadata keys that are set."""
    if not _has_table(conn, "metadata"):
        return {}
    keys = EMBEDDINGS_META_KEYS + (MLX_PARITY_KEY,)
    marks = ",".join("?" * len(keys))
    return {
        str(row[0]): str(row[1]) for row in conn.execute(
            f"SELECT key, value FROM metadata WHERE key IN ({marks})",  # nosec B608
            keys,
        )
    }


def write_embeddings_meta(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    """Upsert ``embeddings_*`` metadata in one transaction (no-op without the table)."""
    if not values or not _has_table(conn, "metadata"):
        return
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in values.items()],
        )
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise


def mlx_parity_for(conn: sqlite3.Connection, model: str) -> bool | None:
    """Recorded MLX parity result for *model* on this machine (None: never checked)."""
    import json

    from .embedding_providers.profiles import platform_key

    raw = read_embeddings_meta(conn).get(MLX_PARITY_KEY)
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("model") != model or record.get("platform") != platform_key():
        return None
    return bool(record.get("ok"))


def _state_values(
    state: str, provider: EmbeddingProvider, resolution: Resolution, stale_count: int,
) -> dict[str, Any]:
    dim = resolution.dim
    # Asking an unloaded sentence-transformers model for its size would load it.
    if not isinstance(provider, LocalEmbeddingProvider) or provider._model is not None:
        dim = provider.dimension
    return {
        "embeddings_state": state,
        "embeddings_provider": provider.name,
        "embeddings_model": resolution.model,
        "embeddings_dim": dim,
        "embeddings_stale_count": stale_count,
    }


def _mlx_model(settings: EmbeddingSettings) -> str:
    from .embedding_providers.profiles import BALANCED_MLX

    return settings.model or BALANCED_MLX.model


def embed_changed(
    store: GraphStore,
    changed_qualified_names: Iterable[str] | None,
    *,
    repo_root: str | Path | None = None,
    wait: bool = True,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | threading.Thread:
    """Embed the changed nodes at low priority; the writer path calls this last.

    Call it after the readiness stamp, under the writer lock. It never touches
    readiness: failures only set ``embeddings_state``.

    Args:
        store: the graph just written. Its database path (not its connection)
            is used; with ``wait=False`` keep the database in place until the
            returned thread finishes.
        changed_qualified_names: nodes added or re-parsed by this update;
            ``None`` embeds every node (first enable, full build).
        repo_root: where ``.code-review-graph.toml`` lives; derived from the
            database path when omitted.
        wait: ``True`` (default) blocks and returns the result dict;
            ``False`` returns the started daemon thread, whose result lands
            in ``thread.result`` (a dict) when it ends.
        env: environment for settings overrides (default ``os.environ``).

    The work runs in its own thread, which lowers only its own priority
    (nice +10 on Linux, utility QoS on macOS) so the caller keeps its
    priority. Model threads are capped at half the cores unless configured.
    Returns ``{"state", "provider", "embedded", "purged", "stale_count",
    "priority"}``; ``state`` is ``off``, ``ready``, ``stale`` or
    ``unavailable`` (plus ``"warning"`` or ``"error"`` when set).
    """
    db_path = Path(store.db_path)
    root = Path(repo_root) if repo_root is not None else repo_root_for_db(db_path)
    names = None if changed_qualified_names is None else sorted(set(changed_qualified_names))
    holder: dict[str, Any] = {}

    def work() -> None:
        from .embedding_providers.priority import lower_current_thread_priority

        priority = lower_current_thread_priority()
        try:
            holder["result"] = _embed_changed_now(db_path, root, names, env)
        except Exception as exc:  # recorded, never raised into the writer path
            logger.warning("Background embedding failed: %s", exc)
            holder["result"] = {"state": "stale", "error": str(exc), "embedded": 0}
        holder["result"]["priority"] = priority

    class _Worker(threading.Thread):
        result: dict[str, Any] | None = None

        def run(self) -> None:
            work()
            self.result = holder.get("result")

    worker = _Worker(name="crg-embed", daemon=True)
    worker.start()
    if not wait:
        return worker
    worker.join()
    return holder["result"]


def _embed_changed_now(
    db_path: Path, repo_root: Path | None, names: list[str] | None,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    settings = load_embedding_settings(repo_root, env)
    graph = GraphStore(db_path)
    try:
        conn = graph._conn
        if not settings.enabled:
            write_embeddings_meta(conn, {"embeddings_state": "off"})
            return {"state": "off", "provider": None, "embedded": 0, "purged": 0,
                    "stale_count": 0}
        provider, resolution = provider_for_settings(
            settings, mlx_parity=mlx_parity_for(conn, _mlx_model(settings)),
        )
        if provider is None:
            if resolution.warning:
                logger.warning("Embeddings unavailable: %s", resolution.warning)
            write_embeddings_meta(conn, {"embeddings_state": "unavailable",
                                         "embeddings_model": resolution.model})
            return {"state": "unavailable", "provider": None, "embedded": 0, "purged": 0,
                    "stale_count": 0, "warning": resolution.warning}

        emb = EmbeddingStore(db_path, embedding_provider=provider, dtype=settings.dtype)
        try:
            purged = emb.purge_orphans()
            if names is None:
                nodes = graph.get_all_nodes(exclude_files=True)
            else:
                nodes = []
                for i in range(0, len(names), _SQL_CHUNK):
                    chunk = names[i:i + _SQL_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    nodes.extend(
                        graph._row_to_node(row) for row in conn.execute(
                            f"SELECT * FROM nodes WHERE qualified_name IN ({marks})",  # nosec B608
                            chunk,
                        )
                    )
            embedded = emb.embed_nodes(nodes, batch_size=settings.batch_size)
            stale = emb.stale_count(provider.name)
        finally:
            emb.close()
        state = "ready" if stale == 0 else "stale"
        write_embeddings_meta(conn, _state_values(state, provider, resolution, stale))
        result: dict[str, Any] = {
            "state": state, "provider": provider.name, "embedded": embedded,
            "purged": purged, "stale_count": stale,
        }
        if resolution.warning:
            result["warning"] = resolution.warning
        return result
    finally:
        graph.close()


def embeddings_status(db_path: str | Path, repo_root: str | Path | None = None) -> dict[str, Any]:
    """What ``code-review-graph embeddings status`` prints; never loads a model."""
    root = Path(repo_root) if repo_root is not None else repo_root_for_db(db_path)
    settings = load_embedding_settings(root)
    out: dict[str, Any] = {
        "enabled": settings.enabled,
        "profile": settings.profile,
        "settings_source": settings.source,
        "state": "off",
        "backend": None,
        "provider": None,
        "model": None,
        "dim": None,
        "dtype": settings.dtype,
        "vectors": 0,
        "vectors_all_providers": 0,
        "stale_count": 0,
        "disk_bytes": 0,
        "warning": None,
    }
    path = Path(db_path)
    if not path.exists():
        out["warning"] = "no graph database; run `code-review-graph build` first"
        return out
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        meta = read_embeddings_meta(conn)
        has_table = _has_table(conn, "embeddings")
        if has_table:
            count_all, disk_all = conn.execute(
                "SELECT COUNT(*), SUM(LENGTH(vector)) FROM embeddings",
            ).fetchone()
            out["vectors_all_providers"] = int(count_all or 0)
            out["disk_bytes"] = int(disk_all or 0)
        resolution = resolve_profile(
            settings, mlx_parity=mlx_parity_for(conn, _mlx_model(settings)),
        )
        out.update(
            backend=resolution.backend, model=resolution.model, dim=resolution.dim,
            profile=resolution.profile, warning=resolution.warning,
        )
        provider_id = resolution.provider_id
        if resolution.cloud:
            provider_id = meta.get("embeddings_provider", "")
        out["provider"] = provider_id or None
        if has_table and provider_id:
            out["vectors"] = int(conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE provider = ?", (provider_id,),
            ).fetchone()[0])
        if _has_table(conn, "nodes"):
            if has_table and provider_id:
                out["stale_count"] = int(conn.execute(
                    "SELECT COUNT(*) FROM nodes n WHERE n.kind != 'File' AND NOT EXISTS ("
                    "SELECT 1 FROM embeddings e WHERE e.qualified_name = n.qualified_name "
                    "AND e.provider = ?)",
                    (provider_id,),
                ).fetchone()[0])
            else:
                out["stale_count"] = int(conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE kind != 'File'",
                ).fetchone()[0])
        if not settings.enabled:
            out["state"] = "off"
        elif not resolution.available:
            out["state"] = "unavailable"
        elif out["vectors"] and out["stale_count"] == 0:
            out["state"] = "ready"
        else:
            out["state"] = "stale"
    finally:
        conn.close()
    return out


def open_search_store(
    db_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[EmbeddingStore | None, dict[str, Any]]:
    """An :class:`EmbeddingStore` ready to search, or ``None`` and why not.

    An explicit *provider*/*model* (MCP tool arguments) is a per-call opt-in
    and bypasses the settings. Otherwise the repository settings decide; with
    embeddings off no store is opened and no model is loaded. The info dict
    carries ``state`` (off|ready|stale|unavailable), ``provider`` and a
    ``warning`` whenever the caller must fall back to keyword search.
    """
    info: dict[str, Any] = {"state": "off", "provider": None, "warning": None}
    root = Path(repo_root) if repo_root is not None else repo_root_for_db(db_path)
    store: EmbeddingStore | None
    if provider or model:
        store = EmbeddingStore(db_path, provider=provider, model=model)
        if not store.available:
            store.close()
            info.update(state="unavailable",
                        warning=f"embedding provider '{provider or 'local'}' is not available")
            return None, info
    else:
        settings = load_embedding_settings(root, env)
        if not settings.enabled:
            return None, info
        store = None
        try:
            # ``closing`` (not ``with`` alone): a sqlite3 connection used as a
            # context manager only commits, it never closes the handle.
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=5)) as conn:
                parity = mlx_parity_for(conn, _mlx_model(settings))
        except sqlite3.Error:
            parity = None
        prov, resolution = provider_for_settings(settings, mlx_parity=parity)
        if prov is None:
            info.update(state="unavailable", warning=resolution.warning)
            return None, info
        store = EmbeddingStore(db_path, embedding_provider=prov, dtype=settings.dtype)
        info["warning"] = resolution.warning

    assert store is not None and store.provider is not None
    name = store.provider.name
    info["provider"] = name
    if store.count_for_provider(name) == 0:
        store.close()
        info.update(
            state="stale",
            warning=f"no vectors for {name} yet; run `code-review-graph embeddings enable`",
        )
        return None, info
    stale = store.stale_count(name)
    info["state"] = "ready" if stale == 0 else "stale"
    if stale:
        info["stale_count"] = stale
    return store, info


class _NullProvider(EmbeddingProvider):
    """Stands in for maintenance that must never embed."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no embedding provider configured")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("no embedding provider configured")

    @property
    def dimension(self) -> int:
        return 0

    @property
    def name(self) -> str:
        return "none"


def purge_vectors(db_path: str | Path, provider_name: str | None = None) -> int:
    """Delete stored vectors (all, or one provider's) without resolving a provider."""
    store = EmbeddingStore(db_path, embedding_provider=_NullProvider())
    try:
        return store.remove_provider(provider_name)
    finally:
        store.close()


def embed_all_nodes(graph_store: GraphStore, embedding_store: EmbeddingStore) -> int:
    """Purge deleted nodes, then embed all current non-file nodes."""
    embedding_store.purge_orphans()
    if not embedding_store.available:
        return 0

    all_files = graph_store.get_all_files()
    all_nodes: list[GraphNode] = []
    for f in all_files:
        all_nodes.extend(graph_store.get_nodes_by_file(f))

    return embedding_store.embed_nodes(all_nodes)


def refresh_embeddings(
    graph_store: GraphStore,
    *,
    provider: str,
    model: str,
) -> dict[str, int] | None:
    """Refresh a previously embedded graph under one exact provider identity.

    This function is deliberately not called by default build paths.  Callers
    must supply both provider and model explicitly.  A graph with no existing
    vectors returns before provider resolution, so routine builds cannot load
    a local model, contact a cloud service, or incur API cost.

    Existing vectors must all use the identity resolved from the requested
    provider/model (including the endpoint for OpenAI-compatible providers).
    Refresh never silently migrates an index to another model or endpoint.
    """
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise ValueError(
            "Embedding refresh requires an explicit provider and model.",
        )

    has_table = graph_store._conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'embeddings'",
    ).fetchone()
    if has_table is None:
        return None
    has_rows = graph_store._conn.execute(
        "SELECT 1 FROM embeddings LIMIT 1",
    ).fetchone()
    if has_rows is None:
        return None
    try:
        rows = graph_store._conn.execute(
            "SELECT DISTINCT provider FROM embeddings ORDER BY provider",
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc).lower() and "provider" in str(exc).lower():
            raise ValueError(
                "Embedding refresh refused: existing rows have no provider identity; "
                "run an explicit embed to migrate and rebuild the index.",
            ) from exc
        raise
    identities = {str(row["provider"]) for row in rows}

    embedding_store = EmbeddingStore(
        graph_store.db_path,
        provider=provider,
        model=model,
    )
    try:
        if not embedding_store.available or embedding_store.provider is None:
            raise RuntimeError(
                f"Embedding provider '{provider}' is unavailable in this environment.",
            )
        resolved_identity = embedding_store.provider.name
        if provider == "minimax":
            resolved_model = resolved_identity.partition(":")[2]
            if model != resolved_model:
                raise ValueError(
                    f"MiniMax refresh model must be '{resolved_model}', got '{model}'.",
                )
        if identities != {resolved_identity}:
            existing = ", ".join(sorted(identities))
            raise ValueError(
                "Embedding refresh refused: existing embeddings use "
                f"{existing}; requested provider resolves to {resolved_identity}.",
            )

        purged = embedding_store.purge_orphans()
        all_nodes: list[GraphNode] = []
        for file_path in graph_store.get_all_files():
            all_nodes.extend(graph_store.get_nodes_by_file(file_path))
        embedded = embedding_store.embed_nodes(all_nodes)
        return {"embedded": embedded, "purged": purged}
    finally:
        embedding_store.close()


def semantic_search(
    query: str,
    graph_store: GraphStore,
    embedding_store: EmbeddingStore,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search nodes using vector similarity, falling back to keyword search."""
    if embedding_store.available and embedding_store.count() > 0:
        results = embedding_store.search(query, limit=limit)
        output = []
        for qn, score in results:
            node = graph_store.get_node(qn)
            if node:
                d = node_to_dict(node)
                d["similarity_score"] = round(score, 4)
                output.append(d)
        return output

    # Fallback to keyword search
    nodes = graph_store.search_nodes(query, limit=limit)
    return [node_to_dict(n) for n in nodes]
