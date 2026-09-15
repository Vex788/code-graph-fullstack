"""Tests for code_review_graph/repo_config.py: the [resolvers.jsp] loader.

Plain assert-based; runnable directly with `python3 tests/test_repo_config.py`
or under pytest, matching this repo's existing dual-mode test style (see
tests/test_hybrid_exact_pin.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from code_review_graph.repo_config import JspResolverConfig, load_jsp_resolver_config


def _write(root: Path, text: str) -> None:
    config_dir = root / ".code-review-graph"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(text, encoding="utf-8")


def test_missing_file_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        result = load_jsp_resolver_config(Path(tmp))
    assert result is None
    print("OK: missing config.toml -> None")


def test_missing_resolvers_table_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "[languages.foo]\ngrammar = 'x'\nextensions = ['.x']\n")
        result = load_jsp_resolver_config(root)
    assert result is None
    print("OK: config.toml with no [resolvers] table -> None")


def test_missing_jsp_section_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "[resolvers.other]\nfoo = 'bar'\n")
        result = load_jsp_resolver_config(root)
    assert result is None
    print("OK: [resolvers] present but no [resolvers.jsp] -> None")


def test_malformed_toml_warns_and_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "[resolvers.jsp\nweb_root = \n")  # syntactically broken
        with patch("code_review_graph.repo_config.logger.warning") as warn:
            result = load_jsp_resolver_config(root)
    assert result is None
    assert warn.call_count == 1
    print("OK: malformed TOML -> warns once, behaves as absent, never raises")


def test_malformed_value_type_warns_and_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "[resolvers.jsp]\nweb_root = 5\n")  # wrong type
        with patch("code_review_graph.repo_config.logger.warning") as warn:
            result = load_jsp_resolver_config(root)
    assert result is None
    assert warn.call_count == 1
    print("OK: wrong-typed key -> warns once, whole section treated as absent")


def test_partial_section_fills_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, '[resolvers.jsp]\nweb_root = "pages"\n')
        result = load_jsp_resolver_config(root)
    assert result is not None
    assert result.web_root == "pages"
    # Every other key falls back to its documented default.
    defaults = JspResolverConfig()
    assert result.source_root == defaults.source_root
    assert result.route_annotations == defaults.route_annotations
    assert result.bean_attribute == defaults.bean_attribute
    assert result.bean_package_prefix == defaults.bean_package_prefix
    assert result.dead_url_suffixes == defaults.dead_url_suffixes
    print("OK: partial section overrides one key, defaults fill the rest")


def test_full_section_uses_every_value():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            """
            [resolvers.jsp]
            web_root = "pages"
            source_root = "java"
            route_annotations = ["Route"]
            bean_attribute = "controller"
            bean_package_prefix = "org."
            dead_url_suffixes = [".do"]
            """,
        )
        result = load_jsp_resolver_config(root)
    assert result == JspResolverConfig(
        web_root="pages",
        source_root="java",
        route_annotations=("Route",),
        bean_attribute="controller",
        bean_package_prefix="org.",
        dead_url_suffixes=(".do",),
    )
    print("OK: full section uses every configured value")


def test_enabled_false_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "[resolvers.jsp]\nenabled = false\n")
        result = load_jsp_resolver_config(root)
    assert result == JspResolverConfig(enabled=False)
    print("OK: enabled=false loads, every other key keeps its default")


def test_context_paths_normalize_to_slash_segment_form():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            '[resolvers.jsp]\ncontext_paths = ["myapp", "/ctx/", "deep/nested"]\n',
        )
        result = load_jsp_resolver_config(root)
    assert result is not None
    assert result.context_paths == ("/myapp", "/ctx", "/deep/nested")
    assert result.enabled is True  # untouched key keeps its default
    print("OK: context_paths normalize to '/segment' form")


def test_enabled_with_wrong_type_warns_and_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, '[resolvers.jsp]\nenabled = "yes"\n')  # wrong type
        with patch("code_review_graph.repo_config.logger.warning") as warn:
            result = load_jsp_resolver_config(root)
    assert result is None
    assert warn.call_count == 1
    print("OK: non-boolean enabled -> warns once, whole section treated as absent")


def test_context_paths_with_wrong_type_warns_and_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, '[resolvers.jsp]\ncontext_paths = ["/ok", 5]\n')  # wrong type
        with patch("code_review_graph.repo_config.logger.warning") as warn:
            result = load_jsp_resolver_config(root)
    assert result is None
    assert warn.call_count == 1
    print("OK: wrong-typed context_paths -> warns once, whole section treated as absent")


def test_empty_context_paths_list_keeps_the_default():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, '[resolvers.jsp]\ncontext_paths = []\n')
        result = load_jsp_resolver_config(root)
    assert result is not None
    assert result.context_paths == ()
    print("OK: an explicitly empty context_paths list is accepted as 'no context paths'")


def test_result_is_cached_until_file_changes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, '[resolvers.jsp]\nweb_root = "pages"\n')
        first = load_jsp_resolver_config(root)
        second = load_jsp_resolver_config(root)
        assert first is second  # served from cache, not re-parsed

        import time

        time.sleep(0.01)
        _write(root, '[resolvers.jsp]\nweb_root = "templates"\n')
        third = load_jsp_resolver_config(root)
        assert third is not None and third.web_root == "templates"
    print("OK: cache keyed on (mtime_ns, size); a real edit is picked up")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(tests)} repo_config tests passed")


if __name__ == "__main__":
    _run_all()
