"""Tracked ``.code-review-graph.toml`` settings: loader, env overrides, writer."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from code_review_graph import repo_settings as rs

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture(autouse=True)
def _fresh_cache():
    rs.clear_cache()
    yield
    rs.clear_cache()


def _write(root: Path, text: str) -> Path:
    path = root / rs.SETTINGS_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_off_without_file(tmp_path):
    settings = rs.load_embedding_settings(tmp_path, env={})
    assert settings.enabled is False
    assert settings.profile == "balanced"
    assert settings.dtype == "float16"
    assert settings.source == "default"
    assert settings.effective_threads == max(1, (__import__("os").cpu_count() or 2) // 2)


def test_file_values_are_read(tmp_path):
    _write(tmp_path, """
[embeddings]
enabled = true
profile = "fast"
model = "my/model"
dim = 128
dtype = "float32"
batch_size = 16
threads = 3
idle_unload_s = 30
""")
    s = rs.load_embedding_settings(tmp_path, env={})
    assert (s.enabled, s.profile, s.model, s.dim, s.dtype) == (True, "fast", "my/model", 128,
                                                             "float32")
    assert (s.batch_size, s.threads, s.idle_unload_s, s.source) == (16, 3, 30.0, "file")
    assert s.effective_threads == 3


def test_invalid_values_warn_and_fall_back(tmp_path, caplog):
    _write(tmp_path, """
[embeddings]
enabled = "yes"
profile = "turbo"
dim = -4
dtype = "int8"
idle_unload_s = -1
""")
    with caplog.at_level(logging.WARNING):
        s = rs.load_embedding_settings(tmp_path, env={})
    assert s == rs.EmbeddingSettings()
    for key in ("enabled", "profile", "dim", "dtype", "idle_unload_s"):
        assert key in caplog.text


def test_malformed_file_is_ignored(tmp_path, caplog):
    _write(tmp_path, "[embeddings\nenabled = true\n")
    with caplog.at_level(logging.WARNING):
        assert rs.load_embedding_settings(tmp_path, env={}).enabled is False
    assert "Malformed TOML" in caplog.text


@pytest.mark.parametrize("value,enabled,profile", [
    ("off", False, "fast"),
    ("0", False, "fast"),
    ("on", True, "fast"),
    ("1", True, "fast"),
    ("balanced", True, "balanced"),
    ("ACCURATE", True, "accurate"),
    ("legacy", True, "legacy"),
    ("openai", True, "openai"),
])
def test_env_overrides_file(tmp_path, value, enabled, profile):
    _write(tmp_path, "[embeddings]\nenabled = false\nprofile = \"fast\"\n")
    s = rs.load_embedding_settings(tmp_path, env={"CRG_EMBEDDINGS": value})
    assert (s.enabled, s.profile, s.source) == (enabled, profile, "env")


def test_unknown_env_value_is_ignored(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        s = rs.load_embedding_settings(tmp_path, env={"CRG_EMBEDDINGS": "maybe"})
    assert s.enabled is False and "CRG_EMBEDDINGS" in caplog.text


def test_env_dim_override(tmp_path):
    s = rs.load_embedding_settings(tmp_path, env={"CRG_EMBEDDING_DIM": "128"})
    assert s.dim == 128
    assert rs.load_embedding_settings(tmp_path, env={"CRG_EMBEDDING_DIM": "x"}).dim is None


@pytest.mark.parametrize("profile,applies", [
    ("legacy", True), ("local", True), ("fast", False), ("balanced", False),
])
def test_env_model_only_for_sentence_transformer_profiles(tmp_path, profile, applies):
    # Harness adapters export CRG_EMBEDDING_MODEL globally for the legacy model.
    env = {"CRG_EMBEDDINGS": profile, "CRG_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5"}
    s = rs.load_embedding_settings(tmp_path, env=env)
    assert (s.model == "BAAI/bge-small-en-v1.5") is applies


def test_cache_follows_file_changes(tmp_path):
    path = _write(tmp_path, "[embeddings]\nenabled = true\n")
    assert rs.load_embedding_settings(tmp_path, env={}).enabled is True
    path.write_text("[embeddings]\nenabled = false  # longer text changes the size\n")
    assert rs.load_embedding_settings(tmp_path, env={}).enabled is False


def test_get_section_dotted(tmp_path):
    _write(tmp_path, "[resolvers.jsp]\nweb_root = \"webapp\"\n")
    assert rs.get_section(tmp_path, "resolvers.jsp") == {"web_root": "webapp"}
    assert rs.get_section(tmp_path, "resolvers.stripes") is None
    assert rs.get_section(None, "embeddings") is None


def test_resolver_table_prefers_tracked_file_then_legacy(tmp_path):
    legacy = tmp_path / ".code-review-graph" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("[resolvers.jsp]\nweb_root = \"legacy\"\n")
    table, source = rs.resolver_table(tmp_path, "jsp")
    assert table == {"web_root": "legacy"} and source == legacy

    _write(tmp_path, "[resolvers.jsp]\nweb_root = \"tracked\"\n")
    table, source = rs.resolver_table(tmp_path, "jsp")
    assert table == {"web_root": "tracked"} and source == tmp_path / rs.SETTINGS_FILENAME
    assert rs.resolver_table(tmp_path, "stripes") == (None, None)


def test_legacy_jsp_config_loader_unchanged(tmp_path):
    from code_review_graph import repo_config

    repo_config.clear_cache()
    legacy = tmp_path / ".code-review-graph" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("[resolvers.jsp]\nweb_root = \"webapp\"\n")
    _write(tmp_path, "[embeddings]\nenabled = true\n")
    config = repo_config.load_jsp_resolver_config(tmp_path)
    assert config is not None and config.web_root == "webapp"


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

EXISTING = """\
# Project graph settings
[resolvers.jsp]
web_root = "web"  # the webapp root

[embeddings]
# pick one of fast | balanced
profile = "fast"   # tuned for laptops
enabled = false

[extra]
keep = [1, 2]
"""


def test_update_existing_section_preserves_everything_else():
    out = rs.update_section(EXISTING, "embeddings", {"enabled": True, "profile": "balanced"})
    assert out == EXISTING.replace(
        'profile = "fast"   # tuned for laptops', 'profile = "balanced"  # tuned for laptops',
    ).replace("enabled = false", "enabled = true")
    parsed = tomllib.loads(out)
    assert parsed["embeddings"] == {"profile": "balanced", "enabled": True}
    assert parsed["extra"] == {"keep": [1, 2]}


def test_update_adds_missing_key_inside_section():
    out = rs.update_section(EXISTING, "embeddings", {"dim": 128})
    lines = out.splitlines()
    assert lines[lines.index("enabled = false") + 1] == "dim = 128"
    assert lines[lines.index("dim = 128") + 1] == ""
    assert tomllib.loads(out)["embeddings"]["dim"] == 128


def test_update_removes_key_with_none():
    out = rs.update_section(EXISTING, "embeddings", {"profile": None})
    assert "profile" not in tomllib.loads(out)["embeddings"]
    assert "# pick one of fast | balanced" in out


def test_update_appends_new_section():
    out = rs.update_section('[resolvers.jsp]\nweb_root = "web"', "embeddings",
                            {"enabled": True, "profile": "fast"})
    assert out == ('[resolvers.jsp]\nweb_root = "web"\n\n[embeddings]\n'
                   'enabled = true\nprofile = "fast"\n')
    assert rs.update_section("", "embeddings", {"enabled": False}) == (
        "[embeddings]\nenabled = false\n"
    )


def test_update_keeps_hash_inside_string_values():
    text = '[embeddings]\nmodel = "org/a#b"  # note\n'
    out = rs.update_section(text, "embeddings", {"model": "org/c#d"})
    assert out == '[embeddings]\nmodel = "org/c#d"  # note\n'


def test_update_refuses_dotted_or_inline_forms():
    with pytest.raises(rs.SettingsWriteError):
        rs.update_section("embeddings.enabled = true\n", "embeddings", {"enabled": False})
    with pytest.raises(rs.SettingsWriteError):
        rs.update_section("embeddings = { enabled = true }\n", "embeddings", {"enabled": False})


def test_write_section_is_atomic_and_invalidates_cache(tmp_path):
    path = _write(tmp_path, EXISTING)
    assert rs.load_embedding_settings(tmp_path, env={}).enabled is False
    rs.write_section(tmp_path, "embeddings", {"enabled": True})
    assert rs.load_embedding_settings(tmp_path, env={}).enabled is True
    assert "# the webapp root" in path.read_text()
    assert not list(tmp_path.glob(".crg-settings-*"))


def test_write_section_creates_file(tmp_path):
    path = rs.write_section(tmp_path, "embeddings", {"enabled": True, "profile": "legacy"})
    assert path.read_text() == '[embeddings]\nenabled = true\nprofile = "legacy"\n'
