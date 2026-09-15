"""Regression tests for safe, connected Ansible graph extraction."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, NodeInfo


def _qualified(node: NodeInfo) -> str:
    if node.kind == "File":
        return node.file_path
    if node.parent_name:
        return f"{node.file_path}::{node.parent_name}.{node.name}"
    return f"{node.file_path}::{node.name}"


def test_ordinary_yaml_in_tasks_directory_yields_only_the_file_marker() -> None:
    # Path-promoted but content-rejected: docker-compose-style YAML inside an
    # Ansible directory contract keeps exactly its File marker (language
    # "ansible") — visible to inventory and coverage, with zero invented
    # task structure.
    parser = CodeParser()
    source = b"""\
- name: frontend
  image: nginx:latest
  ports:
    - 8080
"""

    nodes, edges = parser.parse_bytes(
        Path("roles/example/tasks/application.yaml"),
        source,
    )

    assert len(nodes) == 1
    marker = nodes[0]
    assert marker.kind == "File"
    assert marker.language == "ansible"
    assert marker.name == "roles/example/tasks/application.yaml"
    assert marker.line_start == 1
    assert marker.line_end == source.count(b"\n") + 1
    assert edges == []


def test_empty_yaml_at_ansible_path_yields_only_the_file_marker() -> None:
    parser = CodeParser()
    for source in (b"", b"\n", b"# only a comment\n"):
        nodes, edges = parser.parse_bytes(Path("playbooks/site.yml"), source)

        assert len(nodes) == 1
        assert nodes[0].kind == "File"
        assert nodes[0].language == "ansible"
        assert edges == []


def test_real_playbook_at_promoted_path_still_extracts_tasks() -> None:
    # The content-rejection guard must not swallow real playbooks: hosts +
    # tasks still extract full ansible structure with resolved edges.
    parser = CodeParser()
    source = b"""\
- name: Deploy application
  hosts: all
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hi
"""

    nodes, edges = parser.parse_bytes(Path("playbooks/deploy.yml"), source)

    tasks = [node for node in nodes if node.extra.get("ansible_kind") == "task"]
    assert len(tasks) == 1
    assert tasks[0].language == "ansible"
    assert any(node.kind == "File" for node in nodes)
    assert edges


def test_ansible_relationships_reference_real_unique_nodes(tmp_path: Path) -> None:
    parser = CodeParser()
    path = Path("playbooks/deploy.yml")
    source = b"""\
- name: Deploy application
  hosts: all
  tasks:
    - name: Restart application
      ansible.builtin.debug:
        msg: first
      notify: Reload application
    - name: Restart application
      ansible.builtin.debug:
        msg: second
  handlers:
    - name: Restart service
      listen: Reload application
      ansible.builtin.service:
        name: application
        state: restarted
"""

    nodes, edges = parser.parse_bytes(path, source)

    tasks = [
        node
        for node in nodes
        if node.extra.get("ansible_kind") == "task"
    ]
    assert len(tasks) == 2
    assert len({node.name for node in tasks}) == 2
    assert all(node.name.startswith("Restart application") for node in tasks)

    qualified = {_qualified(node) for node in nodes}
    internal_edges = [
        edge
        for edge in edges
        if edge.kind == "CONTAINS"
        or edge.extra.get("ansible_kind") == "notify"
    ]
    assert internal_edges
    assert all(edge.source in qualified for edge in internal_edges)
    assert all(edge.target in qualified for edge in internal_edges)

    store = GraphStore(tmp_path / "graph.db")
    try:
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)

        notify = next(
            edge
            for edge in internal_edges
            if edge.extra.get("ansible_kind") == "notify"
        )
        assert store.get_node(notify.source) is not None
        handler = store.get_node(notify.target)
        assert handler is not None
        assert handler.extra["ansible_kind"] == "handler"
    finally:
        store.close()
