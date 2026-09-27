"""Smoke test for the per-stage timing harness that produced docs/perf/baseline.json."""

from __future__ import annotations

import json
from pathlib import Path

BASELINE = Path(__file__).resolve().parents[2] / "docs" / "perf" / "baseline.json"
_STAGES = ("full_build", "noop_update", "one_file_update")


def test_stage_timing_reports_every_stage_on_a_small_fixture():
    from code_review_graph.eval.benchmarks.stage_timing import run

    report = run(fork=None, scale=60, repeat=1)
    target = report["targets"]["fixture_scale_60"]
    assert target["graph"]["parse_errors"] == 0
    assert target["graph"]["files"] >= 50
    assert set(_STAGES) <= set(target)
    assert target["one_file_update"]["files_updated"] >= 1
    assert "fts_s" in target["full_build"]["postprocess_timing"]


def test_committed_baseline_has_both_targets():
    report = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert report["baseline"]["ref"] == "wave0-green"
    assert {"fork", "fixture_scale_2000"} <= set(report["targets"])
    for target in report["targets"].values():
        assert set(_STAGES) <= set(target)
