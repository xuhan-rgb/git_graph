from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os
import subprocess

import generate



def git(repo: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, env=env).strip()



def commit(repo: Path, message: str, when: str, file_name: str, content: str) -> None:
    (repo / file_name).write_text(content, encoding="utf-8")
    git(repo, "add", file_name)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    git(repo, "commit", "-m", message, env=env)



def build_graph(repo: Path) -> Tuple[List[dict], List[dict]]:
    cfg = generate.Config(
        repo=repo,
        branches=generate.assign_colors(["main", "feature/demo"], []),
        remotes=[],
        since="2026-04-01",
        output=repo / "out.html",
        fetch=False,
    )
    cfg.local_only_tags = set()

    commits = generate.load_commits(cfg)
    tips, colors = generate.resolve_tips(commits, cfg)
    keep = generate.select_interesting(commits, tips)
    reach = generate.compute_reach_branches(commits, keep, tips)
    lanes = generate.compute_lanes(reach, cfg)
    rows = generate.compute_rows(commits, keep)
    nodes = generate.build_nodes(commits, keep, tips, colors, reach, lanes, rows, cfg)
    segments = generate.walk_segments(commits, keep, reach, cfg)
    return nodes, segments



def setup_merge_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "checkout", "-q", "-b", "main")

    commit(repo, "init", "2026-04-20T10:00:00", "base.txt", "root\n")

    git(repo, "checkout", "-q", "-b", "feature/demo")
    commit(repo, "feature work", "2026-04-21T10:00:00", "feature.txt", "feature\n")

    git(repo, "checkout", "-q", "main")
    commit(repo, "main work", "2026-04-22T10:00:00", "main.txt", "main\n")
    git(repo, "merge", "--no-ff", "feature/demo", "-m", "Merge branch 'feature/demo' into 'main'")
    return repo



def graph_subjects(tmp_path: Path) -> Tuple[Dict[str, dict], List[dict]]:
    repo = setup_merge_repo(tmp_path)
    nodes, segments = build_graph(repo)
    return {node["subject"]: node for node in nodes}, segments



def test_merge_keeps_feature_history_on_feature_lane(tmp_path: Path) -> None:
    nodes_by_subject, _ = graph_subjects(tmp_path)

    feature_node = nodes_by_subject["feature work"]

    assert feature_node["lane"] == 1
    assert feature_node["branches"] == ["feature/demo"]



def test_merge_commit_stays_on_main_lane_and_is_shared(tmp_path: Path) -> None:
    nodes_by_subject, _ = graph_subjects(tmp_path)

    merge_node = nodes_by_subject["Merge branch 'feature/demo' into 'main'"]

    assert merge_node["lane"] == 0
    assert merge_node["branches"] == ["main", "feature/demo"]



def test_merge_edge_to_feature_only_belongs_to_feature_branch(tmp_path: Path) -> None:
    nodes_by_subject, segments = graph_subjects(tmp_path)
    merge_sha = nodes_by_subject["Merge branch 'feature/demo' into 'main'"]["sha"]
    feature_sha = nodes_by_subject["feature work"]["sha"]

    merge_to_feature = next(
        segment
        for segment in segments
        if segment["child"] == merge_sha and segment["parent"] == feature_sha
    )

    assert merge_to_feature["branches"] == ["feature/demo"]
