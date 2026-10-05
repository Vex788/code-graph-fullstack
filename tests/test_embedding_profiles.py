"""Embedding profiles: resolution matrix and backends against mocked model packages.

No test here downloads or loads a real model.
"""

from __future__ import annotations

import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from code_review_graph import embeddings
from code_review_graph.embedding_providers import backends, profiles
from code_review_graph.repo_settings import EmbeddingSettings

try:
    import numpy as np
except ImportError:  # the dev-only lane has no embedding extras
    np = None

needs_numpy = pytest.mark.skipif(np is None, reason="local backends need numpy")

MAC = "darwin-arm64"
LINUX = "linux-x86_64"
MAC_INTEL = "darwin-x86_64"


def _has(*modules):
    return lambda name: name in modules


# ---------------------------------------------------------------------------
# Resolution matrix: platform x installed backends x profile x parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile,platform,installed,parity,backend,served,note", [
    ("fast", LINUX, ("model2vec",), None, "model2vec", "fast", None),
    ("fast", MAC, ("model2vec",), None, "model2vec", "fast", None),
    ("fast", LINUX, (), None, None, "fast", "embeddings-fast"),
    # balanced: MLX on Apple Silicon unless a recorded parity check against ONNX failed
    ("balanced", MAC, ("mlx_embeddings", "fastembed"), True, "mlx", "balanced", None),
    ("balanced", MAC, ("mlx_embeddings", "fastembed"), None, "mlx", "balanced", None),
    ("balanced", MAC, ("mlx_embeddings", "fastembed"), False, "onnx", "balanced", "parity"),
    ("balanced", MAC, ("mlx_embeddings",), None, "mlx", "balanced", None),
    ("balanced", MAC, ("mlx_embeddings",), True, "mlx", "balanced", None),
    ("balanced", MAC, ("fastembed",), None, "onnx", "balanced", None),
    ("balanced", LINUX, ("mlx_embeddings", "fastembed"), True, "onnx", "balanced", None),
    ("balanced", MAC_INTEL, ("fastembed",), None, "onnx", "balanced", None),
    ("balanced", LINUX, (), None, None, "balanced", "embeddings-onnx"),
    ("balanced", MAC, (), None, None, "balanced", "embeddings-mlx,embeddings-onnx"),
    # accurate: Apple Silicon only, otherwise reported fallback to balanced
    ("accurate", MAC, ("mlx_lm",), None, "mlx-lm", "accurate", None),
    ("accurate", MAC, (), None, None, "accurate", "embeddings-mlx"),
    ("accurate", LINUX, ("mlx_lm", "fastembed"), None, "onnx", "balanced", "using 'balanced'"),
    ("accurate", LINUX, (), None, None, "balanced", "using 'balanced'"),
    ("legacy", LINUX, ("sentence_transformers",), None, "sentence-transformers", "legacy",
     None),
    ("legacy", LINUX, (), None, None, "legacy", "[embeddings]"),
])
def test_resolution_matrix(profile, platform, installed, parity, backend, served, note):
    res = profiles.resolve_profile(
        EmbeddingSettings(enabled=True, profile=profile),
        platform=platform, has_module=_has(*installed), mlx_parity=parity,
    )
    assert res.profile == served
    assert res.available is (backend is not None)
    assert (res.spec.backend if res.spec else None) == backend
    if note is None:
        assert res.warning is None
    else:
        assert note in (res.warning or "")


@pytest.mark.parametrize("profile,installed,expected", [
    ("fast", "model2vec", "model2vec:minishlab/potion-code-16M-v2:f32:d256"),
    ("balanced", "fastembed", "onnx:google/embeddinggemma-300m:fp32:d256"),
    ("accurate", "mlx_lm", "mlx-lm:mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ:q4-dwq:d512"),
    ("legacy", "sentence_transformers", "local:BAAI/bge-small-en-v1.5"),
])
def test_provider_ids_encode_backend_model_quant_dim(profile, installed, expected):
    res = profiles.resolve_profile(
        EmbeddingSettings(enabled=True, profile=profile), platform=MAC,
        has_module=_has(installed),
    )
    assert res.provider_id == expected


def test_mlx_balanced_provider_id_differs_from_onnx():
    settings = EmbeddingSettings(enabled=True, profile="balanced")
    mlx = profiles.resolve_profile(settings, platform=MAC, mlx_parity=True,
                                   has_module=_has("mlx_embeddings", "fastembed"))
    onnx = profiles.resolve_profile(settings, platform=LINUX,
                                    has_module=_has("mlx_embeddings", "fastembed"))
    assert mlx.provider_id == "mlx:mlx-community/embeddinggemma-300m-bf16:bf16:d256"
    assert mlx.provider_id != onnx.provider_id


def test_dim_override_truncates_and_changes_identity():
    res = profiles.resolve_profile(
        EmbeddingSettings(enabled=True, profile="balanced", dim=128),
        platform=LINUX, has_module=_has("fastembed"),
    )
    assert res.dim == 128 and res.provider_id.endswith(":d128") and res.warning is None


@pytest.mark.parametrize("profile,installed,dim", [
    ("balanced", "fastembed", 4096),  # above the native size
    ("legacy", "sentence_transformers", 128),  # not truncatable
])
def test_unsupported_dim_falls_back_with_warning(profile, installed, dim):
    res = profiles.resolve_profile(
        EmbeddingSettings(enabled=True, profile=profile, dim=dim),
        platform=LINUX, has_module=_has(installed),
    )
    assert res.available and res.dim == res.spec.default_dim
    assert f"dim={dim}" in res.warning


def test_model_override_applies_to_requested_profile_only():
    settings = EmbeddingSettings(enabled=True, profile="accurate", model="my/qwen")
    on_mac = profiles.resolve_profile(settings, platform=MAC, has_module=_has("mlx_lm"))
    assert on_mac.model == "my/qwen"
    fallback = profiles.resolve_profile(settings, platform=LINUX, has_module=_has("fastembed"))
    assert fallback.profile == "balanced" and fallback.model == "google/embeddinggemma-300m"


def test_cloud_profile_resolves_without_spec():
    res = profiles.resolve_profile(EmbeddingSettings(enabled=True, profile="voyage"))
    assert res.cloud and res.available and res.spec is None and res.backend == "voyage"


def test_platform_key_normalizes_aarch64(monkeypatch):
    monkeypatch.setattr(profiles._platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    assert profiles.platform_key() == "linux-arm64"


def test_module_available_does_not_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "json.tool", raising=False)
    assert profiles.module_available("json.tool")
    assert "json.tool" not in sys.modules
    assert not profiles.module_available("surely_not_installed_crg_pkg")


# ---------------------------------------------------------------------------
# Backends with mocked model packages
# ---------------------------------------------------------------------------

def _vec(*values):
    return np.array(values, dtype=np.float32)


class _FakeMx(ModuleType):
    float32 = "float32"

    def __init__(self):
        super().__init__("mlx.core")
        self.cleared = 0

    def clear_cache(self):
        self.cleared += 1

    def array(self, value):
        return np.array(value)

    def arange(self, n):
        return np.arange(n)


def _mx_array(values):
    """A numpy array whose ``astype`` accepts mlx dtypes, like an mx.array."""

    class _MxArray(np.ndarray):
        def astype(self, dtype, *args, **kwargs):
            return np.asarray(self, dtype=np.float32)

    return np.asarray(values).view(_MxArray)


@pytest.fixture
def fake_mlx(monkeypatch):
    mx = _FakeMx()
    mlx = ModuleType("mlx")
    mlx.core = mx
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    return mx


@needs_numpy
def test_mlx_embeddings_provider_prompts_truncates_and_releases(monkeypatch, fake_mlx):
    calls: list[list[str]] = []
    net_args: list[tuple] = []

    class _Net:
        # gemma3_text.Model.__call__(inputs, attention_mask): a keyword
        # `input_ids` call (mlx-embeddings 0.1.0 generate) must die here
        def __call__(self, inputs, attention_mask=None):
            net_args.append((inputs, attention_mask))
            out = np.zeros((len(inputs), 768), dtype=np.float32)
            out[:, 0], out[:, 1], out[:, 300] = 3.0, 4.0, 100.0  # beyond the 256 prefix
            return SimpleNamespace(text_embeds=_mx_array(out))

    def tokenizer(texts, return_tensors, padding, truncation, max_length, pad_to_multiple_of=None):
        assert return_tensors == "mlx" and padding and truncation
        assert pad_to_multiple_of == backends.MLX_PAD_MULTIPLE
        calls.append(list(texts))
        n = max(len(t) for t in texts)
        return {"input_ids": np.full((len(texts), n), 1),
                "attention_mask": np.ones((len(texts), n))}

    loads: list[str] = []
    mod = ModuleType("mlx_embeddings")
    mod.load = lambda name: loads.append(name) or (_Net(), tokenizer)
    monkeypatch.setitem(sys.modules, "mlx_embeddings", mod)

    provider = backends.MlxEmbeddingsProvider(
        profiles.BALANCED_MLX, profiles.BALANCED_MLX.model, 256, idle_unload_s=0,
    )
    assert not provider.loaded and loads == []
    docs = provider.embed_documents(["a", "b"])
    query = provider.embed_query("find x")
    assert loads == [profiles.BALANCED_MLX.model]
    assert calls == [["title: none | text: a", "title: none | text: b"],
                     ["task: search result | query: find x"]]
    assert all(args[1] is not None for args in net_args)
    assert len(docs[0]) == 256 and docs[0][:2] == pytest.approx([0.6, 0.8])
    assert query[:2] == pytest.approx([0.6, 0.8])
    provider.unload()
    assert not provider.loaded and fake_mlx.cleared == 1


@needs_numpy
def test_mlx_lm_provider_pools_last_token_with_end_token(monkeypatch, fake_mlx):
    class Tok:
        eos_token_id = 7
        unk_token_id = 0

        def convert_tokens_to_ids(self, token):
            assert token == "<|endoftext|>"
            return 9

        def encode(self, text):
            return [len(word) + 10 for word in text.split()] + [9]

    class Net:
        def __init__(self):
            self.batches = []

        def model(self, batch):
            batch = np.asarray(batch)
            self.batches.append(batch)
            hidden = np.zeros(batch.shape + (1024,), dtype=np.float32)
            # Position p carries the token id in dim 0 and the position in dim 1.
            hidden[..., 0] = batch
            hidden[..., 1] = np.arange(batch.shape[1])
            return _mx_array(hidden)

    net = Net()
    mod = ModuleType("mlx_lm")
    mod.load = lambda name: (net, SimpleNamespace(_tokenizer=Tok()))
    monkeypatch.setitem(sys.modules, "mlx_lm", mod)

    provider = backends.MlxLmProvider(profiles.ACCURATE_MLX, "q", 512, idle_unload_s=0)
    vectors = provider.embed_documents_array(["aa b", "a"])
    batch = net.batches[0]
    # Batches run shortest first; the rows come back in input order below.
    assert batch.tolist() == [[11, 9, 9], [12, 11, 9]]  # right-padded with the end token
    # Pooled rows are the last real token (the end token) of each sequence.
    raw = np.array([[9.0, 2.0], [9.0, 1.0]])  # input order: "aa b" then "a"
    expected = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    assert vectors.shape == (2, 512)
    assert vectors[:, :2] == pytest.approx(expected)
    provider.embed_query("x")
    query_ids = net.batches[1][0].tolist()
    assert query_ids[-1] == 9 and len(query_ids) == len(backends.QWEN_QUERY.split()) + 1


@needs_numpy
def test_model2vec_provider_never_forces_download(monkeypatch):
    seen: dict = {}

    class StaticModel:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            seen["load"] = (name, kwargs)
            return cls()

        def encode(self, texts, **kwargs):
            seen["encode"] = kwargs
            return np.ones((len(texts), 256), dtype=np.float32)

    mod = ModuleType("model2vec")
    mod.StaticModel = StaticModel
    monkeypatch.setitem(sys.modules, "model2vec", mod)
    provider = backends.Model2VecProvider(profiles.FAST, profiles.FAST.model, 256,
                                          idle_unload_s=0)
    out = provider.embed_documents_array(["x"])
    assert seen["load"] == (profiles.FAST.model, {"force_download": False})
    assert seen["encode"]["use_multiprocessing"] is False
    assert np.linalg.norm(out[0]) == pytest.approx(1.0)
    assert provider.name == "model2vec:minishlab/potion-code-16M-v2:f32:d256"


@needs_numpy
def test_fastembed_provider_uses_gemma_prompts_and_threads(monkeypatch):
    seen: dict = {"texts": []}

    class TextEmbedding:
        def __init__(self, model_name, threads):
            seen["init"] = (model_name, threads)

        def embed(self, texts, batch_size):
            seen["texts"].append(list(texts))
            return iter([np.ones(768, dtype=np.float32) for _ in texts])

    mod = ModuleType("fastembed")
    mod.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    provider = backends.FastEmbedProvider(profiles.BALANCED_ONNX, profiles.BALANCED_ONNX.model,
                                          256, threads=3, batch_size=1, idle_unload_s=0)
    provider.embed_documents(["a", "bb"])
    provider.embed_query("q")
    assert seen["init"] == ("google/embeddinggemma-300m", 3)
    assert seen["texts"] == [["title: none | text: a"], ["title: none | text: bb"],
                             ["task: search result | query: q"]]


class _CountingProvider(backends.LocalModelProvider):
    loads = 0

    def _load(self):
        type(self).loads += 1
        return object()

    def _encode(self, model, texts, query):
        return np.ones((len(texts), 4), dtype=np.float32)


class _LengthProvider(backends.LocalModelProvider):
    def _load(self):
        return object()

    def _encode(self, model, texts, query):
        return np.array([[float(len(t)), 1.0] for t in texts], dtype=np.float32)


@needs_numpy
def test_sorted_batches_keep_the_input_order():
    provider = _LengthProvider(profiles.FAST, "m", 2, batch_size=2, idle_unload_s=0)
    texts = ["x" * n for n in (9, 1, 5, 3, 7)]
    got = provider.embed_documents_array(texts)
    expected = backends.truncate_normalize([[float(n), 1.0] for n in (9, 1, 5, 3, 7)], 2)
    assert got.tolist() == expected.tolist()


@needs_numpy
def test_idle_unload_releases_the_model():
    provider = _CountingProvider(profiles.FAST, "m", 4, idle_unload_s=0.05)
    provider.embed_query("x")
    assert provider.loaded
    deadline = time.monotonic() + 2
    while provider.loaded and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not provider.loaded
    provider.embed_query("y")  # loads again on demand
    assert provider.loaded
    provider.unload()


@needs_numpy
def test_truncate_normalize_keeps_zero_rows_and_rejects_short_vectors():
    out = backends.truncate_normalize([[0.0, 0.0, 1.0], [3.0, 4.0, 9.0]], 2)
    assert out.tolist() == [[0.0, 0.0], pytest.approx([0.6, 0.8])]
    with pytest.raises(ValueError):
        backends.truncate_normalize([[1.0]], 2)


@needs_numpy
def test_parity_cosine_is_the_worst_text():
    a = _CountingProvider(profiles.FAST, "a", 4, idle_unload_s=0)
    b = _CountingProvider(profiles.FAST, "b", 4, idle_unload_s=0)
    assert backends.parity_cosine(a, b) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Provider construction, locking and priority
# ---------------------------------------------------------------------------

def test_provider_for_settings_is_lazy_and_shared(monkeypatch):
    monkeypatch.setattr(profiles, "module_available", lambda name: name == "model2vec")
    monkeypatch.setattr(embeddings, "_PROFILE_PROVIDERS", {})
    settings = EmbeddingSettings(enabled=True, profile="fast", idle_unload_s=0)
    first, res = embeddings.provider_for_settings(settings)
    second, _ = embeddings.provider_for_settings(settings)
    assert first is second and not first.loaded
    assert first.name == res.provider_id


def test_provider_for_settings_reports_missing_backend(monkeypatch):
    monkeypatch.setattr(profiles, "module_available", lambda name: False)
    provider, res = embeddings.provider_for_settings(EmbeddingSettings(enabled=True,
                                                                       profile="fast"))
    assert provider is None and not res.available and "embeddings-fast" in res.warning


def test_provider_for_settings_cloud_missing_env_is_unavailable(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    provider, res = embeddings.provider_for_settings(EmbeddingSettings(enabled=True,
                                                                       profile="voyage"))
    assert provider is None and "VOYAGE_API_KEY" in res.warning


def test_availability_check_does_not_wait_for_a_model_load():
    """A model load holds the global lock; the availability probe must not queue."""
    held = threading.Event()
    release = threading.Event()

    def loader():
        with embeddings._MODEL_INIT_LOCK:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=loader)
    thread.start()
    try:
        assert held.wait(2)
        started = time.monotonic()
        embeddings._check_available()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        thread.join()


def test_lower_thread_priority_touches_only_the_calling_thread():
    import os

    from code_review_graph.embedding_providers.priority import lower_current_thread_priority

    if not sys.platform.startswith("linux"):
        pytest.skip("per-thread nice is Linux-specific")
    before = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    result: dict = {}

    def worker():
        result["applied"] = lower_current_thread_priority()
        result["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert result["applied"].startswith("nice+")
    assert result["nice"] >= before
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == before
