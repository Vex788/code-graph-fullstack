"""Local model backends for the embedding profiles.

Every backend import is lazy: this module imports only numpy-free stdlib at
load time, and a backend package is imported the first time its model is
needed. Each provider has its own lock (never the process-wide
sentence-transformers lock), releases its model after ``idle_unload_s``
seconds without use, and returns L2-normalized vectors truncated to the
configured dimension.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Any, Optional, Sequence

from ..embeddings import EmbeddingProvider
from .profiles import BackendSpec

logger = logging.getLogger(__name__)

# EmbeddingGemma prompts (fastembed's model description for google/embeddinggemma-300m).
GEMMA_QUERY = "task: search result | query: {text}"
GEMMA_DOCUMENT = "title: none | text: {text}"
# Qwen3-Embedding: instruction on queries only, documents stay raw.
QWEN_QUERY = (
    "Instruct: Given a code search query, retrieve the code symbols that match it\n"
    "Query:{text}"
)
MAX_TOKENS = 512
MLX_CACHE_BYTES = 512 * 1024 * 1024  # Metal buffer cache cap for the MLX embedder
MLX_PAD_MULTIPLE = 32  # few distinct batch shapes keep the cache small


def truncate_normalize(matrix: Any, dim: int) -> Any:
    """First *dim* components of each row, rescaled to unit length (zero rows stay zero)."""
    import numpy as np

    mat = np.asarray(matrix, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    if mat.shape[1] < dim:
        raise ValueError(f"model returned {mat.shape[1]}-d vectors, {dim}-d requested")
    mat = np.array(mat[:, :dim], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    nonzero = norms[:, 0] > 0
    mat[nonzero] /= norms[nonzero]
    return mat


class LocalModelProvider(EmbeddingProvider):
    """Lazy-loading local model with idle unload; subclasses load and encode."""

    query_template = "{text}"
    document_template = "{text}"

    def __init__(
        self,
        spec: BackendSpec,
        model: str,
        dim: int,
        *,
        threads: int = 1,
        batch_size: int = 64,
        idle_unload_s: float = 600.0,
    ) -> None:
        self.spec = spec
        self.model_name = model
        self._dim = dim
        self._threads = max(1, threads)
        self._batch_size = max(1, batch_size)
        self._idle_unload_s = idle_unload_s
        self._lock = threading.RLock()
        self._model: Any = None
        self._timer: Optional[threading.Timer] = None
        self.load_seconds: Optional[float] = None

    @property
    def name(self) -> str:
        return f"{self.spec.backend}:{self.model_name}:{self.spec.quant}:d{self._dim}"

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _encode(self, model: Any, texts: list[str], query: bool) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _release(self) -> None:
        """Backend-specific cleanup after the model reference is dropped."""

    def unload(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._model is None:
                return
            self._model = None
            self._release()
        gc.collect()
        logger.info("Unloaded embedding model %s", self.name)

    def _arm_idle_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._idle_unload_s > 0:
            timer = threading.Timer(self._idle_unload_s, self.unload)
            timer.daemon = True
            timer.start()
            self._timer = timer

    def _vectors(self, texts: Sequence[str], query: bool) -> Any:
        import numpy as np

        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        template = self.query_template if query else self.document_template
        prompted = [template.format(text=text) for text in texts]
        chunks = []
        with self._lock:
            if self._model is None:
                started = time.perf_counter()
                self._model = self._load()
                self.load_seconds = time.perf_counter() - started
                logger.info("Loaded embedding model %s in %.1fs", self.name, self.load_seconds)
            # Length-sorted batches keep padding small; the rows are restored below.
            order = sorted(range(len(prompted)), key=lambda i: len(prompted[i]))
            for i in range(0, len(order), self._batch_size):
                batch = [prompted[j] for j in order[i:i + self._batch_size]]
                chunks.append(np.asarray(self._encode(self._model, batch, query),
                                         dtype=np.float32))
            self._arm_idle_timer()
        matrix = np.vstack(chunks)
        restored = np.empty_like(matrix)
        restored[order] = matrix
        return truncate_normalize(restored, self._dim)

    def embed_documents_array(self, texts: Sequence[str]) -> Any:
        return self._vectors(list(texts), query=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents_array(texts).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._vectors([text], query=True)[0].tolist()


class Model2VecProvider(LocalModelProvider):
    """Static (lookup-table) embeddings: numpy only, identical on every OS."""

    def _load(self) -> Any:
        from model2vec import StaticModel

        # force_download defaults to True, which would re-fetch on every load.
        return StaticModel.from_pretrained(self.model_name, force_download=False)

    def _encode(self, model: Any, texts: list[str], query: bool) -> Any:
        return model.encode(
            texts, show_progress_bar=False, use_multiprocessing=False,
            batch_size=len(texts),
        )


class FastEmbedProvider(LocalModelProvider):
    """EmbeddingGemma through fastembed's ONNX Runtime pipeline."""

    query_template = GEMMA_QUERY
    document_template = GEMMA_DOCUMENT

    def _load(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=self.model_name, threads=self._threads)

    def _encode(self, model: Any, texts: list[str], query: bool) -> Any:
        import numpy as np

        return np.stack(list(model.embed(texts, batch_size=len(texts))))


def _mlx_clear_cache() -> None:
    try:
        import mlx.core as mx
    except ImportError:
        return
    clear = getattr(mx, "clear_cache", None)
    if clear is None:
        clear = getattr(getattr(mx, "metal", None), "clear_cache", None)
    if clear is not None:
        clear()


class MlxEmbeddingsProvider(LocalModelProvider):
    """EmbeddingGemma on Apple Silicon through ``mlx-embeddings``."""

    query_template = GEMMA_QUERY
    document_template = GEMMA_DOCUMENT

    def _load(self) -> Any:
        import mlx.core as mx
        from mlx_embeddings import load

        # Batch shapes vary with text length; an unbounded Metal buffer cache grows
        # by gigabytes during a full index.
        limit = getattr(mx, "set_cache_limit", None) or getattr(
            getattr(mx, "metal", None), "set_cache_limit", None)
        if limit is not None:
            limit(MLX_CACHE_BYTES)
        return load(self.model_name)

    def _encode(self, model: Any, texts: list[str], query: bool) -> Any:
        import mlx.core as mx
        import numpy as np

        net, tokenizer = model
        # mlx-embeddings 0.1.0's generate() splats the HF tokenizer's
        # `input_ids` key into gemma3_text.Model.__call__(inputs=...) and dies
        # on every batch; tokenize here and call the net positionally.
        batch = tokenizer(
            texts, return_tensors="mlx", padding=True,
            truncation=True, max_length=MAX_TOKENS, pad_to_multiple_of=MLX_PAD_MULTIPLE,
        )
        outputs = net(batch["input_ids"], batch.get("attention_mask"))
        return np.array(outputs.text_embeds.astype(mx.float32))

    def _release(self) -> None:
        _mlx_clear_cache()


class MlxLmProvider(LocalModelProvider):
    """Qwen3-Embedding on Apple Silicon through ``mlx-lm`` with last-token pooling."""

    query_template = QWEN_QUERY
    end_token = "<|endoftext|>"  # nosec B105 - tokenizer sentinel, not a secret

    def _load(self) -> Any:
        from mlx_lm import load

        return load(self.model_name)

    def _token_ids(self, tokenizer: Any, texts: list[str]) -> list[list[int]]:
        hf = getattr(tokenizer, "_tokenizer", tokenizer)
        end_id = hf.convert_tokens_to_ids(self.end_token)
        if end_id is None or end_id == getattr(hf, "unk_token_id", None):
            end_id = hf.eos_token_id
        sequences = []
        for text in texts:
            ids = list(hf.encode(text))
            if ids and ids[-1] == end_id:
                ids = ids[:-1]
            # The model pools the hidden state of the trailing end token.
            sequences.append(ids[: MAX_TOKENS - 1] + [end_id])
        return sequences

    def _encode(self, model: Any, texts: list[str], query: bool) -> Any:
        import mlx.core as mx
        import numpy as np

        net, tokenizer = model
        sequences = self._token_ids(tokenizer, texts)
        width = max(len(seq) for seq in sequences)
        pad = sequences[0][-1]
        # Right padding is safe: causal attention keeps pads out of earlier positions.
        batch = mx.array([seq + [pad] * (width - len(seq)) for seq in sequences])
        hidden = net.model(batch)
        last = mx.array([len(seq) - 1 for seq in sequences])
        pooled = hidden[mx.arange(len(sequences)), last]
        return np.array(pooled.astype(mx.float32))

    def _release(self) -> None:
        _mlx_clear_cache()


BACKEND_CLASSES: dict[str, type[LocalModelProvider]] = {
    "model2vec": Model2VecProvider,
    "onnx": FastEmbedProvider,
    "mlx": MlxEmbeddingsProvider,
    "mlx-lm": MlxLmProvider,
}

# Short code/NL snippets for the MLX-vs-ONNX parity check at `embeddings enable`.
PARITY_TEXTS = (
    "VendorInvoiceActionBean.save saves a vendor invoice",
    "def parse_file(path): read bytes, hash, then parse",
    "UserDao.findByEmail returns the user with this email",
    "OrderService place order and reserve stock",
    "jsp page renders the order view form",
    "class HttpClient: retry with exponential backoff",
    "SELECT * FROM invoice WHERE vendor_id = ?",
    "validate credit card number before payment",
)


def parity_cosine(candidate: LocalModelProvider, reference: LocalModelProvider) -> float:
    """Lowest per-text cosine between two providers' document vectors."""
    import numpy as np

    a = candidate.embed_documents_array(PARITY_TEXTS)
    b = reference.embed_documents_array(PARITY_TEXTS)
    if a.shape != b.shape:
        return 0.0
    return float(np.min(np.sum(a * b, axis=1)))


__all__ = [
    "BACKEND_CLASSES",
    "FastEmbedProvider",
    "GEMMA_DOCUMENT",
    "GEMMA_QUERY",
    "LocalModelProvider",
    "MlxEmbeddingsProvider",
    "MlxLmProvider",
    "Model2VecProvider",
    "PARITY_TEXTS",
    "QWEN_QUERY",
    "parity_cosine",
    "truncate_normalize",
]
