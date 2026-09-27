"""``code-review-graph embeddings enable|disable|status`` with stub providers."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from code_review_graph import cli, embeddings
from code_review_graph.embedding_providers import backends, profiles
from code_review_graph.embedding_providers import cli as emb_cli
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.repo_settings import EmbeddingSettings, clear_cache

EXISTING_TOML = """\
# team settings
[resolvers.jsp]
web_root = "webapp"  # keep me
"""


class _Stub(embeddings.EmbeddingProvider):
    def embed(self, texts):
        return [[1.0, float(len(t) % 3), 0.5] for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]

    @property
    def dimension(self):
        return 3

    @property
    def name(self):
        return "stub:cli:f32:d3"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("CRG_EMBEDDINGS", "CRG_EMBEDDING_MODEL", "CRG_EMBEDDING_DIM", "CRG_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_cache()
    embeddings.clear_matrix_cache()
    yield
    clear_cache()


@pytest.fixture
def stub_provider(monkeypatch):
    provider = _Stub()

    def fake(settings: EmbeddingSettings, *, mlx_parity=None):
        return provider, profiles.Resolution(settings.profile, settings.profile, profiles.FAST,
                                             "stub", 3, True)

    monkeypatch.setattr(embeddings, "provider_for_settings", fake)
    return provider


def _repo(tmp_path: Path, *, graph: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".code-review-graph.toml").write_text(EXISTING_TOML)
    if graph:
        (repo / ".code-review-graph").mkdir()
        store = GraphStore(repo / ".code-review-graph" / "graph.db")
        for name in ("VendorInvoice", "OrderService"):
            store.upsert_node(NodeInfo(kind="Class", name=name, file_path=str(repo / "A.java"),
                                       line_start=1, line_end=2, language="java"))
        store.commit()
        store.close()
    return repo


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "embeddings", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return int(exc.value.code or 0), capsys.readouterr().out


def test_status_without_graph(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, graph=False)
    code, out = _run(monkeypatch, capsys, "status", "--repo", str(repo), "--json")
    status = json.loads(out)
    assert code == 0 and status["state"] == "off" and status["enabled"] is False
    assert "build" in status["warning"]


def test_enable_no_embed_writes_settings_and_keeps_other_content(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, graph=False)
    code, out = _run(monkeypatch, capsys, "enable", "--profile", "fast", "--no-embed",
                     "--repo", str(repo))
    assert code == 0 and "Enabled embeddings (profile fast)" in out
    text = (repo / ".code-review-graph.toml").read_text()
    assert text.startswith(EXISTING_TOML)
    assert text.endswith('[embeddings]\nenabled = true\nprofile = "fast"\n')


def test_enable_builds_first_vectors(tmp_path, monkeypatch, capsys, stub_provider):
    repo = _repo(tmp_path)
    code, out = _run(monkeypatch, capsys, "enable", "--profile", "fast", "--repo", str(repo),
                     "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["result"]["state"] == "ready" and payload["result"]["embedded"] == 2
    db = repo / ".code-review-graph" / "graph.db"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 2
        meta = dict(conn.execute("SELECT key, value FROM metadata WHERE key LIKE 'embeddings_%'"))
    assert meta["embeddings_state"] == "ready" and meta["embeddings_provider"] == "stub:cli:f32:d3"


def test_enable_keeps_configured_profile(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, graph=False)
    (repo / ".code-review-graph.toml").write_text('[embeddings]\nprofile = "legacy"\n')
    code, _ = _run(monkeypatch, capsys, "enable", "--no-embed", "--repo", str(repo))
    assert code == 0
    assert 'profile = "legacy"' in (repo / ".code-review-graph.toml").read_text()


def test_enable_reports_env_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRG_EMBEDDINGS", "off")
    repo = _repo(tmp_path)
    code, out = _run(monkeypatch, capsys, "enable", "--repo", str(repo))
    assert code == 3 and "CRG_EMBEDDINGS=off overrides the file" in out


def test_enable_with_missing_backend_is_degraded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(profiles, "module_available", lambda name: False)
    repo = _repo(tmp_path)
    code, out = _run(monkeypatch, capsys, "enable", "--profile", "fast", "--repo", str(repo))
    assert code == 3 and "unavailable" in out and "embeddings-fast" in out


def test_disable_purge_and_status(tmp_path, monkeypatch, capsys, stub_provider):
    repo = _repo(tmp_path)
    _run(monkeypatch, capsys, "enable", "--profile", "fast", "--repo", str(repo))
    code, out = _run(monkeypatch, capsys, "disable", "--purge", "--repo", str(repo), "--json")
    payload = json.loads(out)
    assert code == 0 and payload["purged"] == 2
    toml = (repo / ".code-review-graph.toml").read_text()
    assert "enabled = false" in toml and "# keep me" in toml
    code, out = _run(monkeypatch, capsys, "status", "--repo", str(repo))
    assert code == 0 and out.startswith("Embeddings: off")
    with sqlite3.connect(repo / ".code-review-graph" / "graph.db") as conn:
        state = conn.execute(
            "SELECT value FROM metadata WHERE key = 'embeddings_state'").fetchone()[0]
    assert state == "off"


def test_status_human_output(tmp_path, monkeypatch, capsys, stub_provider):
    monkeypatch.setattr(profiles, "module_available", lambda name: name == "model2vec")
    repo = _repo(tmp_path)
    _run(monkeypatch, capsys, "enable", "--profile", "fast", "--no-embed", "--repo", str(repo))
    code, out = _run(monkeypatch, capsys, "status", "--repo", str(repo))
    assert code == 0
    for label in ("profile:", "backend:  model2vec", "dim:      256 (float16)", "vectors:",
                  "stale:    2", "disk:"):
        assert label in out


# ---------------------------------------------------------------------------
# MLX parity gate (mocked backends)
# ---------------------------------------------------------------------------

def _fake_backend(vector):
    class Fake(backends.LocalModelProvider):
        def _load(self):
            return object()

        def _encode(self, model, texts, query):
            return np.tile(np.asarray(vector, dtype=np.float32), (len(texts), 1))

    return Fake


@pytest.mark.parametrize("mlx_vec,ok", [
    ([1.0] * 768, True),
    ([1.0] * 128 + [-1.0] * 640, False),  # differs inside the 256-d prefix
])
def test_parity_check_records_result_and_gates_mlx(tmp_path, monkeypatch, mlx_vec, ok):
    repo = _repo(tmp_path)
    db = repo / ".code-review-graph" / "graph.db"
    monkeypatch.setattr(profiles, "platform_key", lambda: profiles.MAC_ARM)
    monkeypatch.setattr(profiles, "module_available", lambda name: True)
    monkeypatch.setattr(backends, "MlxEmbeddingsProvider", _fake_backend(mlx_vec))
    monkeypatch.setattr(backends, "FastEmbedProvider", _fake_backend([1.0] * 768))
    settings = EmbeddingSettings(enabled=True, profile="balanced")

    record = emb_cli.check_mlx_parity(db, settings)
    assert record["ok"] is ok and record["platform"] == "darwin-arm64"
    with sqlite3.connect(db) as conn:
        assert embeddings.mlx_parity_for(conn, profiles.BALANCED_MLX.model) is ok
        # A different MLX model was never checked.
        assert embeddings.mlx_parity_for(conn, "other/model") is None
    res = profiles.resolve_profile(settings, platform=profiles.MAC_ARM,
                                   has_module=lambda name: True, mlx_parity=ok)
    assert res.spec.backend == ("mlx" if ok else "onnx")


def test_parity_check_skips_off_mac(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(profiles, "platform_key", lambda: "linux-x86_64")
    assert emb_cli.check_mlx_parity(repo / ".code-review-graph" / "graph.db",
                                    EmbeddingSettings(enabled=True, profile="balanced")) is None


def test_parity_backend_failure_means_onnx(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    class Broken(backends.LocalModelProvider):
        def _load(self):
            raise RuntimeError("no metal device")

    monkeypatch.setattr(profiles, "platform_key", lambda: profiles.MAC_ARM)
    monkeypatch.setattr(profiles, "module_available", lambda name: True)
    monkeypatch.setattr(backends, "MlxEmbeddingsProvider", Broken)
    monkeypatch.setattr(backends, "FastEmbedProvider", _fake_backend([1.0] * 768))
    record = emb_cli.check_mlx_parity(repo / ".code-review-graph" / "graph.db",
                                      EmbeddingSettings(enabled=True, profile="balanced"))
    assert record["ok"] is False and "no metal device" in record["error"]
