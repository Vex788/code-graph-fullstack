"""crg-heal: scope, status -> action table, locks, fingerprint dedupe, seed validation.

The script runs for real against a stub ``code-review-graph`` whose per-repo status is a small
state machine (``update`` and ``clone-graph`` move it), so no graph is ever cloned or built.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_harness_kit_content import (
    TARGETS,
    _commit,
    _git_repo,
    _script,
    skip_windows,
)

HEAL = "skills/graph-bootstrap/scripts/crg_heal.py"

STUB = """#!{python}
import fcntl, json, os, sys, time

cfg_path = os.environ["STUB_CONFIG"]
argv = sys.argv[1:]


def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def log(path_key, line):
    with open(cfg_path + "." + path_key, "a") as handle:
        handle.write(line + "\\n")


def with_cfg(mutator):
    with open(cfg_path, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        cfg = json.load(handle)
        result = mutator(cfg)
        handle.seek(0)
        handle.truncate()
        json.dump(cfg, handle)
        return result


log("calls", " ".join(argv))
command = argv[0]
if command == "--version":
    print("code-review-graph " + with_cfg(lambda c: c).get("version", "2.3.8+fs.8"))
    sys.exit(0)
if command == "status":
    repo = value("--repo")

    def read(cfg):
        seq = cfg.get("sequence", {{}}).get(repo)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return cfg["status"].get(repo, "missing_graph")

    status = with_cfg(read)
    if status in ("schema_too_new", "error"):
        print(json.dumps({{"status": "error", "error_code": status, "message": "x"}}))
        sys.exit(1)
    cfg = with_cfg(lambda c: c)
    print(json.dumps({{
        "nodes": 10, "files": 2, "repo_root": repo, "built_at_commit": "a" * 40,
        "current_sha": "b" * 40, "failed_files": cfg.get("failed_files", {{}}).get(repo, 0),
        "contract_version": cfg.get("contract_version", "1.0.0-rc1"),
        "readiness": {{"status": status, "embeddings": "off", "reasons": []}},
        "source_identity": {{
            "missing_indexed_paths": cfg.get("gaps", {{}}).get(repo, []),
            "deleted_indexed_paths": cfg.get("deleted", {{}}).get(repo, []),
            "skipped_oversize_paths": cfg.get("oversize", {{}}).get(repo, []),
        }},
    }}))
elif command == "update":
    repo = value("--repo")
    time.sleep(with_cfg(lambda c: c).get("update_sleep", 0))
    once = with_cfg(lambda c: c.pop("update_once", None))
    if once:
        print(once["stderr"], file=sys.stderr)
        sys.exit(once["rc"])
    if with_cfg(lambda c: c).get("update_stderr"):
        print(with_cfg(lambda c: c)["update_stderr"], file=sys.stderr)
    with_cfg(lambda cfg: cfg["status"].__setitem__(
        repo, cfg.get("after_update", {{}}).get(repo, cfg["status"].get(repo))))
    sys.exit(with_cfg(lambda c: c).get("update_rc", 0))
elif command == "clone-graph":
    target = value("--to")
    log("clones", "start %s %f" % (target, time.time()))
    time.sleep(with_cfg(lambda c: c).get("clone_sleep", 0))
    with_cfg(lambda cfg: cfg["status"].__setitem__(
        target, cfg.get("after_clone", {{}}).get(target, "ok")))
    log("clones", "end %s %f" % (target, time.time()))
    print(json.dumps({{"status": "ok", "rows_rewritten": 1}}))
sys.exit(0)
"""


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        projects = self.home / "IdeaProjects"
        projects.mkdir(parents=True)
        self.pms = _git_repo(projects / "pms")
        _commit(self.pms, "init")
        self.spapi = _git_repo(projects / "sp_api_library")
        _commit(self.spapi, "init")
        self.cfg = tmp_path / "stub.json"
        self.cfg.write_text(json.dumps({"status": {}}), encoding="utf-8")
        self.stub = tmp_path / "code-review-graph"
        self.stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        self.stub.chmod(0o755)
        self.script = _script(tmp_path, "claude", HEAL)
        self.state = self.home / ".claude" / "state" / "crg-heal"
        self.reconcile_state = self.home / ".claude" / "state" / "crg-reconcile"

    def worktree(self, name: str, repo: Path | None = None) -> Path:
        repo = repo or self.pms
        path = self.home / "IdeaProjects" / name
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                        "b-" + name.strip("."), str(path)],
                       check=True, timeout=30)
        return path.resolve()

    def configure(self, **fields) -> None:
        cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if isinstance(value, dict):
                cfg.setdefault(key, {}).update({str(k): v for k, v in value.items()})
            else:
                cfg[key] = value
        self.cfg.write_text(json.dumps(cfg), encoding="utf-8")

    def env(self) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.home), "CRG_BIN": str(self.stub),
                "STUB_CONFIG": str(self.cfg), "CRG_HEAL_POLL_SECONDS": "0.05",
                # The hermetic HOME lives under pytest's tmp dir by construction;
                # without the opt-out the temporary-root guard skips every clone
                # flow under test (on Linux; on macOS the guard misses resolved
                # /private/var/folders and the tests pass for the wrong reason).
                "CRG_ALLOW_TEMPORARY_ROOT": "1"}

    def command(self, repo: Path | str, *args: str) -> list[str]:
        return [sys.executable, str(self.script), "--repo", str(repo), "--json", *args]

    def run(self, repo: Path | str, *args: str) -> tuple[int, dict]:
        done = subprocess.run(self.command(repo, *args), capture_output=True, text=True,
                              timeout=120, env=self.env(), check=False)
        assert done.stdout.strip(), done.stderr
        return done.returncode, json.loads(done.stdout)

    def calls(self) -> list[str]:
        path = Path(str(self.cfg) + ".calls")
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def clone_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("clone-graph")]

    def lock_path(self, repo: Path) -> Path:
        return self.state / (hashlib.sha1(str(repo).encode()).hexdigest()[:12] + ".lock")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path.resolve())


pytestmark = skip_windows


def test_ok_graph_is_a_noop(rig: Rig):
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(rig.pms)
    assert rc == 0
    assert (report["before"], report["after"], report["action"]) == ("ok", "ok", "none")
    assert report["usable"] is True and report["claim_scope"] == "full"
    assert report["healed"] is False and report["gaps"] == []
    assert set(report["receipt"]) == {"built_at_commit", "current_sha", "nodes", "files"}
    assert report["repo_root"] == str(rig.pms)
    assert [c.split()[0] for c in rig.calls()] == ["status"]


def test_partial_index_is_usable_and_reports_gaps(rig: Rig):
    rig.configure(status={rig.pms: "partial_index"}, failed_files={rig.pms: 2},
                  gaps={rig.pms: ["web/a.jsp"]})
    rc, report = rig.run(rig.pms)
    assert rc == 3 and report["action"] == "none"
    assert report["usable"] is True and report["claim_scope"] == "degraded"
    assert {"path": "web/a.jsp", "kind": "missing"} in report["gaps"]
    assert any(g["kind"] == "failed_files" and g["count"] == 2 for g in report["gaps"])
    assert [c.split()[0] for c in rig.calls()] == ["status"]


def test_stale_graph_runs_one_bounded_update(rig: Rig):
    rig.configure(status={rig.pms: "stale_graph"}, after_update={rig.pms: "ok"})
    rc, report = rig.run(rig.pms)
    assert rc == 0 and report["action"] == "update" and report["healed"] is True
    assert (report["before"], report["after"]) == ("stale_graph", "ok")
    updates = [c for c in rig.calls() if c.startswith("update")]
    assert updates == [f"update --skip-flows --if-locked=wait --lock-wait 60 --repo {rig.pms}"]


def test_stale_graph_that_stays_stale_is_degraded_usable(rig: Rig):
    rig.configure(status={rig.pms: "stale_graph"}, update_rc=75)
    rc, report = rig.run(rig.pms)
    assert rc == 3 and report["usable"] is True and report["healed"] is False
    assert report["claim_scope"] == "degraded" and report["after"] == "stale_graph"


STUB_TOOL_ID = "code-review-graph 2.3.8+fs.8|1.0.0-rc1"


def _shell_fingerprint(repo: Path, tool_id: str = STUB_TOOL_ID) -> str:
    """crg-reconcile's fingerprint(): git state plus '<--version output>|<contract version>'."""
    script = (f'{{ git -C "{repo}" rev-parse HEAD; git -C "{repo}" status --porcelain; '
              f'git -C "{repo}" diff HEAD; echo "{tool_id}"; }} 2>/dev/null '
              f'| shasum | cut -c1-16')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


def test_stale_worktree_updates_once_per_fingerprint(rig: Rig):
    rig.configure(status={rig.pms: "stale_worktree"})
    rc, first = rig.run(rig.pms)
    assert rc == 3 and first["action"] == "update" and first["usable"] is True
    assert first["fingerprint"] == _shell_fingerprint(rig.pms)
    state = (rig.reconcile_state / hashlib.sha1(str(rig.pms).encode()).hexdigest()[:12])
    status, fingerprint, stamp = state.read_text(encoding="utf-8").split()
    assert (status, fingerprint) == ("stale_worktree", first["fingerprint"]) and int(stamp) > 0

    rc, second = rig.run(rig.pms)
    assert rc == 3 and second["action"] == "none" and "fingerprint" in second["next"]
    assert len([c for c in rig.calls() if c.startswith("update")]) == 1

    (rig.pms / "new.jsp").write_text("<p/>\n", encoding="utf-8")
    rc, third = rig.run(rig.pms)
    assert third["action"] == "update" and third["fingerprint"] != first["fingerprint"]
    assert len([c for c in rig.calls() if c.startswith("update")]) == 2


@pytest.mark.parametrize(("field", "upgraded", "tool_id"), [
    ("version", "2.3.9+fs.9", "code-review-graph 2.3.9+fs.9|1.0.0-rc1"),
    ("contract_version", "1.1.0", "code-review-graph 2.3.8+fs.8|1.1.0"),
])
def test_tool_upgrade_rearms_the_stale_worktree_update(rig: Rig, field: str, upgraded: str,
                                                       tool_id: str):
    rig.configure(status={rig.pms: "stale_worktree"})
    rc, first = rig.run(rig.pms)
    assert first["action"] == "update" and first["fingerprint"] == _shell_fingerprint(rig.pms)
    rc, again = rig.run(rig.pms)
    assert again["action"] == "none" and again["fingerprint"] == first["fingerprint"]

    rig.configure(**{field: upgraded})
    rc, rearmed = rig.run(rig.pms)
    assert rearmed["action"] == "update"
    assert rearmed["fingerprint"] == _shell_fingerprint(rig.pms, tool_id)
    assert rearmed["fingerprint"] != first["fingerprint"]
    assert len([c for c in rig.calls() if c.startswith("update")]) == 2


def test_unreadable_tool_version_still_dedupes(rig: Rig):
    """A tool whose --version prints nothing keys the attempt on 'unknown', not on a crash."""
    rig.stub.write_text(rig.stub.read_text(encoding="utf-8").replace(
        'print("code-review-graph " + with_cfg(lambda c: c).get("version", "2.3.8+fs.8"))', "pass"),
        encoding="utf-8")
    rig.configure(status={rig.pms: "stale_worktree"})
    rc, first = rig.run(rig.pms)
    assert first["action"] == "update"
    assert first["fingerprint"] == _shell_fingerprint(rig.pms, "unknown|1.0.0-rc1")
    rc, second = rig.run(rig.pms)
    assert second["action"] == "none"


def test_deleted_only_stale_worktree_is_healed_once_per_fingerprint(rig: Rig):
    """No missing file, only a vanished indexed one: still one update, never a loop."""
    rig.configure(status={rig.pms: "stale_worktree"}, deleted={rig.pms: ["web/gone.jsp"]})
    rc, first = rig.run(rig.pms)
    assert rc == 3 and first["action"] == "update" and first["usable"] is True
    assert first["gaps"] == [{"path": "web/gone.jsp", "kind": "deleted"}]
    rc, second = rig.run(rig.pms)
    assert second["action"] == "none" and "fingerprint" in second["next"]
    assert len([c for c in rig.calls() if c.startswith("update")]) == 1

    rig.configure(after_update={rig.pms: "ok"}, deleted={rig.pms: []})
    (rig.pms / "touch.txt").write_text("x\n", encoding="utf-8")
    rc, healed = rig.run(rig.pms)
    assert rc == 0 and healed["healed"] is True and healed["gaps"] == []


def test_oversize_files_are_reported_as_skipped_gaps(rig: Rig):
    rig.configure(status={rig.pms: "ok"}, oversize={rig.pms: ["gen/Big.java"]})
    rc, report = rig.run(rig.pms)
    assert rc == 0 and report["claim_scope"] == "full" and report["action"] == "none"
    assert report["gaps"] == [{"path": "gen/Big.java", "kind": "skipped_oversize"}]
    assert [c.split()[0] for c in rig.calls()] == ["status"]


def test_stale_worktree_healed_forgets_the_attempt(rig: Rig):
    rig.configure(status={rig.pms: "stale_worktree"}, after_update={rig.pms: "ok"})
    rc, report = rig.run(rig.pms)
    assert rc == 0 and report["healed"] is True
    assert not rig.reconcile_state.exists() or not list(rig.reconcile_state.iterdir())


def test_building_polls_and_never_starts_a_build(rig: Rig):
    rig.configure(sequence={rig.pms: ["building", "building", "ok"]})
    rc, report = rig.run(rig.pms)
    assert rc == 0 and report["action"] == "poll" and report["after"] == "ok"
    assert {c.split()[0] for c in rig.calls()} == {"status"}


def test_building_past_the_budget_is_busy(rig: Rig):
    rig.configure(sequence={rig.pms: ["building"]})
    rc, report = rig.run(rig.pms, "--budget", "1")
    assert rc == 75 and report["usable"] is False and report["after"] == "building"
    assert {c.split()[0] for c in rig.calls()} == {"status"}


def test_rebuild_required_worktree_is_cloned_from_the_validated_seed(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: "rebuild_required"})
    rc, report = rig.run(wt)
    assert rc == 0 and report["action"] == "clone" and report["healed"] is True
    assert (report["before"], report["after"]) == ("rebuild_required", "ok")
    assert rig.clone_calls() == [
        f"clone-graph --from {rig.pms} --to {wt} --json --force --no-update --lock-wait 30"]


WRONG_ROOT_ERROR = (
    "RuntimeError: the graph holds 9 file(s) such as '/Users/x/IdeaProjects/pms/a.java', none of "
    "them under '/tmp/wt'; it was built with a different repository root. Rebuild it, or retry "
    "with the root it was built with, instead of reconciling every file away.")


@pytest.mark.parametrize("status", ["stale_graph", "stale_worktree"])
def test_wrong_root_update_error_forces_a_reclone_from_the_seed(rig: Rig, status: str):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: status},
                  update_once={"rc": 1, "stderr": WRONG_ROOT_ERROR})
    rc, report = rig.run(wt)
    assert rc == 0 and report["action"] == "clone" and report["healed"] is True
    assert (report["before"], report["after"]) == (status, "ok")
    assert [c.split()[0] for c in rig.calls() if c.split()[0] not in ("status", "--version")] == [
        "update", "clone-graph", "update"]
    assert rig.clone_calls() == [
        f"clone-graph --from {rig.pms} --to {wt} --json --force --no-update --lock-wait 30"]


def test_other_update_failures_are_not_reclones(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: "stale_graph"}, update_rc=1,
                  update_stderr="RuntimeError: disk I/O error")
    rc, report = rig.run(wt)
    assert rc == 3 and report["action"] == "update" and rig.clone_calls() == []


def test_wrong_root_with_no_clone_reports_instead_of_cloning(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: "stale_graph"}, update_rc=1,
                  update_stderr=WRONG_ROOT_ERROR)
    rc, report = rig.run(wt, "--no-clone")
    assert rc == 3 and report["action"] == "update" and rig.clone_calls() == []
    assert "different repository root" in report["next"] and "--no-clone" in report["next"]


def test_missing_graph_worktree_is_cloned_without_force(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(wt)
    assert rc == 0 and report["before"] == "missing_graph" and report["action"] == "clone"
    (call,) = rig.clone_calls()
    assert "--force" not in call and f"--to {wt}" in call


def test_update_timeout_after_the_clone_leaves_a_usable_stale_graph(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: "rebuild_required"},
                  after_clone={wt: "stale_graph"}, after_update={wt: "ok"}, update_sleep=10)
    done = subprocess.run(rig.command(wt), capture_output=True, text=True, timeout=120,
                          env={**rig.env(), "CRG_HEAL_UPDATE_SECONDS": "0.5"}, check=False)
    report = json.loads(done.stdout)
    assert done.returncode == 3 and report["action"] == "clone"
    assert (report["before"], report["after"]) == ("rebuild_required", "stale_graph")
    assert report["usable"] is True and report["claim_scope"] == "degraded"
    assert "update timed out" in report["next"]
    assert [c.split()[0] for c in rig.calls() if not c.startswith("status")] == [
        "clone-graph", "update"]
    assert "--no-update" in rig.clone_calls()[0]


def test_building_seed_is_polled_until_ok_before_the_clone(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={wt: "rebuild_required"},
                  sequence={rig.pms: ["building", "building", "ok"]})
    rc, report = rig.run(wt)
    assert rc == 0 and report["action"] == "clone" and report["after"] == "ok"
    assert len(rig.clone_calls()) == 1


def test_seed_that_stays_building_is_refused_within_the_budget(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "building", wt: "rebuild_required"})
    done = subprocess.run(rig.command(wt, "--budget", "2"), capture_output=True, text=True,
                          timeout=120, env=rig.env(), check=False)
    report = json.loads(done.stdout)
    assert done.returncode == 4 and rig.clone_calls() == []
    assert "seed graph is building" in report["next"]


def test_seed_directory_wins_when_it_exists(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    wt = rig.worktree("feature")
    rig.configure(status={seed: "ok", rig.pms: "partial_index"})
    rc, report = rig.run(wt)
    assert rc == 0 and f"--from {seed}" in rig.clone_calls()[0]


def test_seed_without_a_graph_is_cloned_from_the_ok_main_checkout(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(seed)
    assert rc == 0 and report["action"] == "clone" and report["healed"] is True
    assert (report["before"], report["after"]) == ("missing_graph", "ok")
    assert rig.clone_calls() == [
        f"clone-graph --from {rig.pms} --to {seed} --json --no-update --lock-wait 30"]
    assert [c.split()[0] for c in rig.calls() if not c.startswith("status")] == [
        "clone-graph", "update"]


def test_seed_bootstrap_catches_up_when_the_clone_lands_stale(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: "ok"}, after_clone={seed: "stale_graph"},
                  after_update={seed: "ok"})
    rc, report = rig.run(seed)
    assert rc == 0 and report["after"] == "ok" and report["healed"] is True
    assert [c.split()[0] for c in rig.calls() if not c.startswith("status")] == [
        "clone-graph", "update"]
    (update,) = [c for c in rig.calls() if c.startswith("update")]
    assert update == f"update --skip-flows --if-locked=wait --lock-wait 60 --repo {seed}"


@pytest.mark.parametrize("main_status", ["partial_index", "stale_graph", "stale_worktree",
                                         "rebuild_required", "missing_graph"])
def test_seed_bootstrap_needs_an_ok_main_checkout(rig: Rig, main_status: str):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: main_status})
    rc, report = rig.run(seed)
    assert rc == 4 and report["usable"] is False and report["action"] == "none"
    assert report["next"] == "main checkout is not ok: heal it first"
    assert rig.clone_calls() == []


def test_seed_bootstrap_respects_no_clone(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(seed, "--no-clone")
    assert rc == 4 and "--no-clone" in report["next"] and rig.clone_calls() == []


def test_seed_bootstrap_is_capped_by_the_clone_lock(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: "ok"})
    rig.state.mkdir(parents=True)
    with open(rig.state / "clone.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, report = rig.run(seed, "--budget", "1")
    assert rc == 75 and rig.clone_calls() == []


def test_seed_with_rebuild_required_still_waits_for_the_nightly_build(rig: Rig):
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={rig.pms: "ok", seed: "rebuild_required"})
    rc, report = rig.run(seed)
    assert rc == 4 and "crg-postprocess-all" in report["next"] and rig.clone_calls() == []


def test_wrong_root_seed_reports_the_full_seed_rebuild_it_needs(rig: Rig):
    """The seed cannot be re-cloned from itself: name the rebuild instead of the nightly."""
    seed = rig.worktree(".crg-seed-pms")
    rig.configure(status={seed: "stale_graph"},
                  update_once={"rc": 1, "stderr": WRONG_ROOT_ERROR})
    rc, report = rig.run(seed)
    assert rc == 3 and report["action"] == "update" and rig.clone_calls() == []
    assert report["next"].endswith(
        "seed graph root mismatch; a full seed rebuild is required")
    assert "crg-postprocess-all" not in report["next"]


def test_clone_only_clones_a_missing_graph_and_forces_a_rebuild_required_one(rig: Rig):
    fresh, rebuilt = rig.worktree("fresh"), rig.worktree("rebuilt")
    rig.configure(status={rig.pms: "ok", rebuilt: "rebuild_required"})
    for wt in (fresh, rebuilt):
        rc, report = rig.run(wt, "--clone-only")
        assert rc == 0 and report["action"] == "clone" and report["after"] == "ok"
    first, second = rig.clone_calls()
    assert f"--to {fresh}" in first and "--force" not in first
    assert f"--to {rebuilt}" in second and "--force" in second


@pytest.mark.parametrize(("status", "rc"), [("ok", 0), ("stale_graph", 3), ("stale_worktree", 3),
                                            ("partial_index", 3), ("building", 4)])
def test_clone_only_leaves_every_other_status_alone(rig: Rig, status: str, rc: int):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok", wt: status})
    code, report = rig.run(wt, "--clone-only")
    assert code == rc and report["action"] == "none" and report["after"] == status
    assert {c.split()[0] for c in rig.calls()} == {"status"}
    assert "--clone-only" in report["next"] or status == "ok"


def test_clone_only_still_validates_the_seed_and_scope(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "partial_index"})
    rc, report = rig.run(wt, "--clone-only")
    assert rc == 4 and "partial_index" in report["next"] and rig.clone_calls() == []
    rc, report = rig.run(rig.spapi, "--clone-only")
    assert rc == 4 and "crg-postprocess-all" in report["next"]
    rc, report = rig.run(rig.pms.parent / "elsewhere", "--clone-only")
    assert rc == 4 and report["action"] == "out_of_scope"


@pytest.mark.parametrize("seed_status", ["partial_index", "stale_graph", "rebuild_required",
                                         "missing_graph"])
def test_seed_that_is_not_ok_blocks_the_clone(rig: Rig, seed_status: str):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: seed_status, wt: "rebuild_required"})
    rc, report = rig.run(wt)
    assert rc == 4 and report["usable"] is False and report["claim_scope"] == "none"
    assert report["action"] == "none" and seed_status in report["next"]
    assert rig.clone_calls() == []


@pytest.mark.parametrize("status", ["rebuild_required", "missing_graph"])
def test_seed_and_sp_api_library_are_never_cloned(rig: Rig, status: str):
    rig.configure(status={rig.pms: status, rig.spapi: status})
    for root in (rig.pms, rig.spapi):
        rc, report = rig.run(root)
        assert rc == 4 and report["action"] == "none" and report["usable"] is False
    assert rig.clone_calls() == []
    rc, report = rig.run(rig.spapi)
    assert "crg-postprocess-all" in report["next"]


def test_sp_api_library_worktree_is_not_cloned_from_the_pms_seed(rig: Rig):
    wt = rig.worktree("spapi-feature", rig.spapi)
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(wt)
    assert rc == 4 and rig.clone_calls() == []


def test_no_clone_flag_disables_cloning(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok"})
    rc, report = rig.run(wt, "--no-clone")
    assert rc == 4 and rig.clone_calls() == [] and "--no-clone" in report["next"]


@pytest.mark.parametrize("status", ["schema_too_new", "error"])
def test_schema_too_new_and_error_never_update_or_clone(rig: Rig, status: str):
    rig.configure(status={rig.pms: status})
    rc, report = rig.run(rig.pms)
    assert rc == 4 and report["action"] == "none" and report["before"] == status
    assert {c.split()[0] for c in rig.calls()} == {"status"}


def test_out_of_scope_repo_touches_nothing(rig: Rig, tmp_path: Path):
    other = _git_repo(tmp_path / "other")
    rc, report = rig.run(other)
    assert rc == 4 and report["action"] == "out_of_scope" and report["usable"] is False
    assert report["claim_scope"] == "none" and rig.calls() == []
    rc, report = rig.run(rig.pms / "web")
    assert rc == 4 and report["action"] == "out_of_scope"


def test_busy_repo_lock_exits_75_past_the_budget(rig: Rig):
    rig.configure(status={rig.pms: "stale_graph"})
    rig.state.mkdir(parents=True)
    with open(rig.lock_path(rig.pms), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, report = rig.run(rig.pms, "--budget", "1")
    assert rc == 75 and report["action"] == "busy" and report["usable"] is False
    assert rig.calls() == []


def test_busy_clone_cap_exits_75_without_cloning(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok"})
    rig.state.mkdir(parents=True)
    with open(rig.state / "clone.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, report = rig.run(wt, "--budget", "1")
    assert rc == 75 and report["action"] == "busy" and rig.clone_calls() == []
    assert "clone.lock" in report["next"]


def test_two_clones_never_overlap(rig: Rig):
    first, second = rig.worktree("one"), rig.worktree("two")
    rig.configure(status={rig.pms: "ok"}, clone_sleep=1.0)
    procs = [subprocess.Popen(rig.command(wt), stdout=subprocess.PIPE, text=True, env=rig.env())
             for wt in (first, second)]
    reports = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
    assert [p.returncode for p in procs] == [0, 0]
    assert all(r["healed"] for r in reports)
    events = sorted(
        (float(line.split()[2]), line.split()[0])
        for line in Path(str(rig.cfg) + ".clones").read_text(encoding="utf-8").splitlines())
    assert [kind for _, kind in events] == ["start", "end", "start", "end"]


def test_concurrent_heals_of_one_repo_clone_once(rig: Rig):
    wt = rig.worktree("feature")
    rig.configure(status={rig.pms: "ok"}, clone_sleep=1.0)
    procs = [subprocess.Popen(rig.command(wt), stdout=subprocess.PIPE, text=True, env=rig.env())
             for _ in range(2)]
    reports = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
    assert [p.returncode for p in procs] == [0, 0]
    assert sorted(r["action"] for r in reports) == ["clone", "none"]
    assert len(rig.clone_calls()) == 1


def test_help_and_usage(rig: Rig):
    done = subprocess.run([sys.executable, str(rig.script), "--help"], capture_output=True,
                          text=True, timeout=30, env=rig.env(), check=False)
    assert done.returncode == 0 and "--budget" in done.stdout and "--no-clone" in done.stdout
    assert "--clone-only" in done.stdout
    missing = subprocess.run([sys.executable, str(rig.script)], capture_output=True, text=True,
                             timeout=30, env=rig.env(), check=False)
    assert missing.returncode == 2


def test_state_dir_follows_the_target(tmp_path: Path):
    """Each rendered target keeps its locks next to its own crg-update state."""
    expected = {"claude": ".claude/state/crg-heal", "zcode": ".zcode/harness/state/crg-heal"}
    for target, tail in expected.items():
        script = _script(tmp_path, target, HEAL)
        probe = ("import runpy, sys; m = runpy.run_path(sys.argv[1]); "
                 "print(m['state_dir']())")
        out = subprocess.run([sys.executable, "-c", probe, str(script)], capture_output=True,
                             text=True, check=True, timeout=30,
                             env={**os.environ, "HOME": "/h"}).stdout.strip()
        assert out == f"/h/{tail}"


def test_heal_script_is_in_every_target():
    from tests.test_harness_kit_content import rendered

    for target in TARGETS:
        assert HEAL in rendered(target)

