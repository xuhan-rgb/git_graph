#!/usr/bin/env python3
"""
Git 分支拓扑生成器 (通用版)。

特性
----
- 自动发现最近活跃的分支 (committerdate + --active-days)，无需 hardcode
- 简化 DAG：只保留分支头 / tag / 合并点 / 分叉点 / 子图边界，线性提交折叠成段
- 单文件自包含 HTML，不依赖任何 CDN
- 可交互：点击节点看 commit，点击连线展开段内被折叠的提交
- 水平虚线时间线按日期分隔，每个日期独占一个时间条
- 默认 git fetch --prune 获取最新远端状态，并自动清理远端已删除的本地 tag
- 自动从调色板分配颜色；同名的本地/远端分支共享色调

用法
----
    python generate.py                       # 默认：分析当前目录仓库
    python generate.py ../repo               # 分析指定仓库路径
    python generate.py ../repo --days 14     # 只看最近 14 天
    python generate.py ../repo --include-local
    python generate.py ../repo --active-days 30
    python generate.py ../repo --branches main dev
    python generate.py ../repo --no-fetch
    python generate.py ../repo --open
    python generate.py ../repo -o /tmp/graph.html
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# ---- 配色盘：同名的本地/远端分支共享颜色，不同分支依次取下一个 ----------- #
PALETTE = [
    "#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#f06292",
    "#ec407a", "#ffd54f", "#7986cb", "#4db6ac", "#ff8a65",
    "#aed581", "#90a4ae", "#f48fb1", "#9575cd", "#dce775",
    "#64b5f6", "#ffab91", "#a5d6a7",
]

# 主干识别策略：
#   Tier 0 = 分支名(去掉 remote 前缀后) 精确等于这些关键字 —— 最权威的主干
#   Tier 1 = 分支名里包含这些关键字 —— 次级主干 (如 team/main, feature/develop-x)
#   Tier 2 = 其它分支 —— 按 recency 排
# 每一 tier 内部都按 committerdate 降序。
MAIN_EXACT = {"main", "master", "develop", "trunk"}
MAIN_KEYWORDS = ("main", "master", "develop", "trunk")


# -----------------------------------------------------------------------------#
# 基础工具
# -----------------------------------------------------------------------------#

def git(*args: str, repo: Path | None = None) -> str:
    # Only trim trailing newlines; generic strip() would also eat control-field
    # delimiters such as \x1f used by `git log --format`.
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        capture_output=True,
        cwd=str(repo) if repo else None,
    ).stdout.rstrip("\r\n")


def get_remotes(repo: Path) -> list[str]:
    """返回本仓库配置的所有 remote 名称，至少有 origin。"""
    try:
        out = git("remote", repo=repo)
    except subprocess.CalledProcessError:
        return ["origin"]
    return [r for r in out.split("\n") if r] or ["origin"]


def is_remote_ref(name: str, remotes: list[str]) -> bool:
    """判断一个短名是否是 remote-tracking 分支 (如 origin/main)。"""
    return any(name.startswith(r + "/") for r in remotes)


def strip_remote(name: str, remotes: list[str]) -> str:
    """剥掉 remote 前缀，剩下"业务名"用于同名合并 (origin/main → main)。"""
    for r in remotes:
        prefix = r + "/"
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# -----------------------------------------------------------------------------#
# CLI + 配置
# -----------------------------------------------------------------------------#

@dataclass
class Config:
    repo: Path
    branches: list[tuple[str, str]]    # [(ref, color), ...]，lane 从 0 开始
    remotes: list[str]
    since: str                          # YYYY-MM-DD
    output: Path
    fetch: bool
    local_only_tags: set = field(default_factory=set)  # 本地独有、未推送的 tag 名


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Generate an interactive git branch topology HTML.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("repo", nargs="?", default=Path("."), type=Path,
                    help="目标 git 仓库路径，默认当前目录")
    ap.add_argument("--days", type=int, default=30,
                    help="画出最近 N 天内的提交")
    ap.add_argument("--since", default=None,
                    help="起始日期 (YYYY-MM-DD)，覆盖 --days")
    ap.add_argument("--active-days", type=int, default=14,
                    help="自动发现：分支在最近 N 天有提交才视为活跃")
    ap.add_argument("--branches", nargs="+", default=None, metavar="REF",
                    help="显式指定要画的分支列表 (短名)，禁用自动发现")
    ap.add_argument("--include-local", action="store_true",
                    help="自动发现时包含本地分支 (refs/heads/*)")
    ap.add_argument("--max-branches", type=int, default=20,
                    help="自动发现上限：最多画多少条分支")
    ap.add_argument("-o", "--output", type=Path,
                    default=None,
                    help="HTML 输出路径")
    ap.add_argument("--no-fetch", action="store_true",
                    help="跳过 git fetch，直接用本地 remote-tracking 状态")
    ap.add_argument("--open", action="store_true",
                    help="生成后自动在默认浏览器中打开 HTML")
    return ap.parse_args(argv)


# -----------------------------------------------------------------------------#
# 自动发现活跃分支 + 颜色分配
# -----------------------------------------------------------------------------#

def discover_active_branches(
    active_days: int,
    include_local: bool,
    remotes: list[str],
    max_branches: int,
    repo: Path,
) -> list[str]:
    """按 committerdate 降序返回最近 N 天有提交的分支短名。

    默认只返回 remote-tracking 分支；`--include-local` 同时返回本地分支。
    已跳过 `origin/HEAD` 等 symbolic ref。
    """
    cutoff = int(time.time() - active_days * 86400)

    patterns = ["refs/remotes/"]
    if include_local:
        patterns.append("refs/heads/")

    # for-each-ref 不支持 %x1f 转义（那是 git log 的语法），用字面字符即可。
    fmt = "%(refname)|%(committerdate:unix)"
    out = git("for-each-ref", "--sort=-committerdate", f"--format={fmt}", *patterns, repo=repo)

    branches: list[str] = []
    for line in out.split("\n"):
        if not line:
            continue
        refname, ts = line.split("|", 1)
        if int(ts) < cutoff:
            continue
        if refname.endswith("/HEAD"):
            continue

        if refname.startswith("refs/remotes/"):
            short = refname[len("refs/remotes/"):]
        elif refname.startswith("refs/heads/"):
            short = refname[len("refs/heads/"):]
        else:
            continue
        branches.append(short)

    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for b in branches:
        if b not in seen:
            seen.add(b)
            uniq.append(b)

    # 排序：主干精确匹配优先，其次是包含关键字，最后按 recency。
    # uniq 已按 committerdate desc 排过，uniq.index(name) 就是"recency 名次"。
    def sort_key(name: str) -> tuple[int, int, int]:
        base = strip_remote(name, remotes).lower()
        if base in MAIN_EXACT:
            tier = 0
        elif any(kw in base for kw in MAIN_KEYWORDS):
            tier = 1
        else:
            tier = 2
        # 同 tier 内：remote 先于同名 local，保持相邻 (便于视觉配对)
        is_local = not is_remote_ref(name, remotes)
        return (tier, uniq.index(name), 1 if is_local else 0)

    sorted_branches = sorted(uniq, key=sort_key)[:max_branches]
    return sorted_branches


def assign_colors(branches: list[str], remotes: list[str]) -> list[tuple[str, str]]:
    """同名的本地/远端分支共享颜色。"""
    color_by_base: dict[str, str] = {}
    result: list[tuple[str, str]] = []
    idx = 0
    for b in branches:
        base = strip_remote(b, remotes)
        if base not in color_by_base:
            color_by_base[base] = PALETTE[idx % len(PALETTE)]
            idx += 1
        result.append((b, color_by_base[base]))
    return result


def print_branch_summary(branches: list[tuple[str, str]], remotes: list[str], repo: Path) -> None:
    print(f"Discovered {len(branches)} branches to draw:")
    for i, (name, color) in enumerate(branches):
        try:
            date = git("log", "-1", "--format=%ad", "--date=short", name, repo=repo)
        except subprocess.CalledProcessError:
            date = "??"
        marker = "local" if not is_remote_ref(name, remotes) else "remote"
        print(f"  {i:2d}. [{date}] {name:<50} ({marker}, {color})")


# -----------------------------------------------------------------------------#
# 数据加载
# -----------------------------------------------------------------------------#

@dataclass
class Commit:
    sha: str
    parents: list[str]
    subject: str
    author: str
    date: str
    timestamp: int
    refs: str
    children: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:8]


def load_commits(cfg: Config) -> dict[str, Commit]:
    valid_refs: list[str] = []
    for name, _ in cfg.branches:
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", name],
                check=True, capture_output=True, cwd=str(cfg.repo),
            )
            valid_refs.append(name)
        except subprocess.CalledProcessError:
            print(f"[warn] ref 不存在，已跳过: {name}")

    if not valid_refs:
        raise SystemExit("没有任何有效的 ref，无法继续")

    fmt = "%H%x1f%P%x1f%s%x1f%an%x1f%ad%x1f%ct%x1f%D"
    out = git("log", f"--format={fmt}", "--date=short", f"--since={cfg.since}", *valid_refs, repo=cfg.repo)

    commits: dict[str, Commit] = {}
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\x1f", 6)
        if len(parts) != 7:
            raise ValueError(
                f"unexpected git log output: expected 7 fields, got {len(parts)} for {line!r}",
            )
        sha, parents, subj, auth, date, timestamp, refs_str = parts
        commits[sha] = Commit(
            sha=sha,
            parents=parents.split() if parents else [],
            subject=subj,
            author=auth,
            date=date,
            timestamp=int(timestamp),
            refs=refs_str,
        )
    for sha, c in commits.items():
        for p in c.parents:
            if p in commits:
                commits[p].children.append(sha)
    return commits


def resolve_tips(
    commits: dict[str, Commit],
    cfg: Config,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    tips: dict[str, list[str]] = {}
    colors: dict[str, str] = {}
    for name, color in cfg.branches:
        try:
            sha = git("rev-parse", name, repo=cfg.repo)
        except subprocess.CalledProcessError:
            continue
        if sha not in commits:
            continue
        tips.setdefault(sha, []).append(name)
        colors.setdefault(sha, color)
    return tips, colors


def summarize_commit(c: Commit) -> dict[str, Any]:
    return {
        "sha": c.sha,
        "short": c.short,
        "subject": c.subject,
        "author": c.author,
        "date": c.date,
        "timestamp": c.timestamp,
        "refs": c.refs,
    }


def load_commit_summaries(shas: list[str], repo: Path) -> dict[str, dict[str, Any]]:
    uniq = list(dict.fromkeys(sha for sha in shas if sha))
    if not uniq:
        return {}

    fmt = "%H%x1f%s%x1f%an%x1f%ad%x1f%ct%x1f%D"
    out = git("show", "-s", f"--format={fmt}", "--date=short", *uniq, repo=repo)

    result: dict[str, dict[str, Any]] = {}
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\x1f", 5)
        if len(parts) != 6:
            continue
        sha, subj, auth, date, timestamp, refs_str = parts
        result[sha] = {
            "sha": sha,
            "short": sha[:8],
            "subject": subj,
            "author": auth,
            "date": date,
            "timestamp": int(timestamp),
            "refs": refs_str,
        }
    return result


# -----------------------------------------------------------------------------#
# 图简化
# -----------------------------------------------------------------------------#

def select_interesting(commits: dict[str, Commit], tips: dict[str, list[str]]) -> set[str]:
    keep: set[str] = set()
    for sha, c in commits.items():
        if sha in tips:                                 # 分支头
            keep.add(sha); continue
        if len(c.parents) >= 2:                         # 合并点
            keep.add(sha); continue
        if len(c.children) >= 2:                        # 分叉点
            keep.add(sha); continue
        if "tag:" in c.refs:                            # 带 tag
            keep.add(sha); continue
        if not any(p in commits for p in c.parents):    # 子集的根
            keep.add(sha); continue
        if not c.children:                              # 子集的叶
            keep.add(sha); continue
    return keep


def walk_segments(
    commits: dict[str, Commit],
    keep: set[str],
    reach_branches: dict[str, set[str]],
    cfg: Config,
) -> list[dict[str, Any]]:
    """每个 kept 节点沿父方向走链路，遇到下一个 kept 节点即形成一段。"""
    segs: list[dict[str, Any]] = []
    for sha in keep:
        for p in commits[sha].parents:
            if p not in commits:
                continue
            hidden: list[Commit] = []
            cur = p
            steps = 0
            while cur not in keep and steps < 500:
                hidden.append(commits[cur])
                next_parents = [x for x in commits[cur].parents if x in commits]
                if not next_parents:
                    break
                cur = next_parents[0]
                steps += 1
            if cur in keep:
                shared = reach_branches.get(sha, set()) & reach_branches.get(cur, set())
                branch_names = shared or (reach_branches.get(sha, set()) | reach_branches.get(cur, set()))
                segs.append({
                    "child": sha,
                    "parent": cur,
                    "branches": sort_branch_names(branch_names, cfg),
                    "hidden": [
                        {
                            "sha": h.sha, "short": h.short,
                            "subject": h.subject, "author": h.author,
                            "date": h.date, "refs": h.refs,
                        }
                        for h in hidden
                    ],
                })
    return segs


# -----------------------------------------------------------------------------#
# 布局
# -----------------------------------------------------------------------------#

def compute_lanes(
    reach_branches: dict[str, set[str]],
    cfg: Config,
) -> dict[str, int]:
    branch_order = {name: i for i, (name, _) in enumerate(cfg.branches)}

    lane: dict[str, int] = {}
    for sha, branches in reach_branches.items():
        candidates = [branch_order[b] for b in branches if b in branch_order]
        lane[sha] = min(candidates) if candidates else len(cfg.branches)
    return lane


def compute_rows(commits: dict[str, Commit], keep: set[str]) -> dict[str, int]:
    """时间戳降序 (最新在顶部) + 稳定 tiebreaker。

    用 commit timestamp (秒级) 而不是 .date (日级)，避免同一天多个 commit
    被 SHA 字典序错排——例如 merge 节点被推到当天后续 commit 的上方。
    """
    ordered = sorted(keep, key=lambda s: (commits[s].timestamp, s), reverse=True)
    return {sha: i for i, sha in enumerate(ordered)}


# -----------------------------------------------------------------------------#
# 节点构建
# -----------------------------------------------------------------------------#

def parse_tag_names(refs: str) -> list[str]:
    """从 `git log` 的 %D 格式里抽出 tag 名。

    refs 形如："HEAD -> team/main, tag: v0.5.5-r6, origin/team/main"
    """
    tags = []
    for part in refs.split(","):
        part = part.strip()
        if part.startswith("tag:"):
            tags.append(part[4:].strip())
    return tags


def sort_branch_names(names: set[str] | list[str], cfg: Config) -> list[str]:
    branch_order = {name: i for i, (name, _) in enumerate(cfg.branches)}
    return sorted(names, key=lambda name: branch_order.get(name, len(branch_order)))


def compute_reach_branches(
    commits: dict[str, Commit],
    keep: set[str],
    tips: dict[str, list[str]],
) -> dict[str, set[str]]:
    # Phase 1: first-parent walk from each tip → "primary ownership".
    # A commit is "owned" by branch B only if it lies on B's trunk
    # (the chain you get by always following the first parent from B's tip).
    # Merge-introduced ancestors do NOT get the merging branch's label here.
    primary_owner: dict[str, set[str]] = {sha: set() for sha in commits}
    for tip_sha, names in tips.items():
        cur: str | None = tip_sha
        seen: set[str] = set()
        while cur is not None and cur not in seen and cur in commits:
            seen.add(cur)
            primary_owner[cur].update(names)
            parents = [p for p in commits[cur].parents if p in commits]
            cur = parents[0] if parents else None

    # Phase 2: project ownership onto kept commits, with merge propagation.
    # For a merge commit, we want its branches to also include the labels
    # of every branch that flows in via any parent — recursively, so that
    # nested merges (a merge whose parent is itself a merge) propagate too.
    cache: dict[str, set[str]] = {}

    def branches_of(sha: str) -> set[str]:
        if sha in cache:
            return cache[sha]
        result = set(primary_owner[sha])
        parents = commits[sha].parents
        if len(parents) >= 2:
            # If sha is on some trunk, only 2nd+ parents "introduce" branches
            # (the 1st parent just continues the trunk and would over-tag).
            # If sha is itself anonymous (off all trunks), all parents introduce.
            introducing = parents[1:] if result else parents
            for p in introducing:
                if p not in commits:
                    continue
                # Anonymous merge ancestors need recursion to surface their
                # 2nd-parent contributions; trunk parents resolve in O(1).
                result |= primary_owner[p] or branches_of(p)
        cache[sha] = result
        return result

    return {sha: branches_of(sha) for sha in keep}


def build_nodes(
    commits: dict[str, Commit],
    keep: set[str],
    tips: dict[str, list[str]],
    colors: dict[str, str],
    reach_branches: dict[str, set[str]],
    lanes: dict[str, int],
    rows: dict[str, int],
    cfg: Config,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for sha in sorted(keep, key=lambda s: rows[s]):
        c = commits[sha]
        is_merge = len(c.parents) >= 2
        is_split = len(c.children) >= 2
        is_tip = sha in tips
        all_tag_names = parse_tag_names(c.refs)
        tag_names       = [t for t in all_tag_names if t not in cfg.local_only_tags]
        local_tag_names = [t for t in all_tag_names if t in cfg.local_only_tags]
        tip_names = tips.get(sha, [])

        # is_local = 所有 tip 都是本地 ref (没有任何 remote-tracking 指向)
        is_local = bool(tip_names) and all(
            not is_remote_ref(n, cfg.remotes) for n in tip_names
        )

        kind = (
            "merge" if is_merge else
            "split" if is_split else
            "tip"   if is_tip   else
            "tag"   if (tag_names or local_tag_names) else
            "node"
        )
        nodes.append({
            "sha": sha,
            "short": c.short,
            "subject": c.subject,
            "author": c.author,
            "date": c.date,
            "refs": c.refs,
            "lane": lanes[sha],
            "row": rows[sha],
            "tip_names": tip_names,
            "tag_names": tag_names,
            "local_tag_names": local_tag_names,
            "branches": sort_branch_names(reach_branches.get(sha, set()), cfg),
            "is_local": is_local,
            "kind": kind,
            "color": colors.get(sha, ""),
        })
    return nodes


# -----------------------------------------------------------------------------#
# HTML 渲染
# -----------------------------------------------------------------------------#

HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Git 分支拓扑</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh;
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #141821; color: #e6e8ed;
  }
  .wrap { display: flex; height: 100vh; }
  .graph {
    flex: 1; overflow: auto; position: relative;
    cursor: grab; user-select: none;
  }
  .graph.dragging { cursor: grabbing; }
  .toolbar {
    position: sticky; top: 0; z-index: 6;
    background: #1c212c; border-bottom: 1px solid #2e3340;
    box-shadow: 0 8px 20px rgba(8, 10, 16, 0.28);
  }
  .panel {
    width: 440px; background: #1c212c; padding: 16px 20px;
    overflow: auto; border-left: 1px solid #2e3340;
  }
  .legend {
    padding: 10px 16px 8px; background: #1c212c;
    border-bottom: 1px solid #2e3340; font-size: 12px; color: #b0b6c4;
  }
  .legend span { display: inline-block; margin-right: 18px; }
  .legend .dot {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    margin-right: 4px; vertical-align: middle;
  }
  .legend .today-note { color: #ffab91; font-weight: bold; }
  .legend .hint { color: #8bb6ff; }
  .branch-bar {
    display: flex; flex-wrap: wrap; gap: 8px;
    padding: 10px 14px 12px; background: #1a1f29;
  }
  .controls {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 10px 14px 0; background: #1a1f29;
  }
  .controls input {
    min-width: 280px; flex: 1 1 280px;
    border: 1px solid #394256; border-radius: 10px;
    background: #121720; color: #e6e8ed;
    padding: 8px 12px; outline: none;
  }
  .controls input:focus {
    border-color: #80cbc4;
    box-shadow: 0 0 0 3px rgba(128, 203, 196, 0.12);
  }
  button.toggle-chip {
    border: 1px solid #394256; background: transparent; color: #c6d0df;
    border-radius: 999px; padding: 7px 12px; cursor: pointer;
    font: inherit; font-size: 12px;
  }
  button.toggle-chip.is-on {
    border-color: #80cbc4; background: #163032; color: #d6fffb;
  }
  .selection-status {
    font-size: 12px; color: #94a0b4;
  }
  button.branch-chip, button.clear-chip, button.branch-link {
    border: 1px solid #394256; background: #222835; color: #dce2ec;
    border-radius: 999px; padding: 6px 10px; cursor: pointer;
    font: inherit; font-size: 12px; line-height: 1;
    transition: opacity 120ms ease, border-color 120ms ease, transform 120ms ease, background 120ms ease;
  }
  button.branch-chip:hover, button.clear-chip:hover, button.branch-link:hover {
    transform: translateY(-1px); border-color: #80cbc4;
  }
  button.clear-chip { background: transparent; color: #9aa4b8; }
  .branch-swatch {
    display: inline-block; width: 9px; height: 9px; border-radius: 50%;
    margin-right: 6px; vertical-align: -1px;
  }
  .branch-scope {
    margin-left: 6px; color: #92a0b6; font-size: 11px;
  }
  .branch-chip.is-selected, .branch-link.is-selected {
    border-color: #ffd54f; background: #3c3113; color: #fff0b8;
    box-shadow: 0 0 0 1px rgba(255, 213, 79, 0.15);
  }
  .branch-chip.is-primary, .branch-link.is-primary {
    border-color: #ffd54f; background: #3c3113; color: #fff0b8;
    box-shadow: 0 0 0 1px rgba(255, 213, 79, 0.15);
  }
  .branch-chip.is-secondary, .branch-link.is-secondary {
    border-color: #ffab91; background: #3c241e; color: #ffd9cf;
  }
  .branch-chip.is-shared, .branch-link.is-shared {
    border-color: #80cbc4; background: #143337; color: #d6fffb;
    box-shadow: 0 0 0 1px rgba(128, 203, 196, 0.15);
  }
  .branch-chip.is-related, .branch-link.is-related {
    border-color: #80cbc4;
    border-style: dashed;
    background: rgba(20, 51, 55, 0.28);
    color: #a9d7d2;
    box-shadow: none;
  }
  .branch-chip.is-related .branch-scope, .branch-link.is-related .branch-scope {
    color: #7fb2ad;
  }
  .branch-chip.is-dim, .branch-link.is-dim, .clear-chip.is-dim { opacity: 0.34; }
  .branch-chip.is-hidden, .branch-link.is-hidden { display: none; }
  svg { display: block; }
  circle.node, polygon.node, rect.node { cursor: pointer; stroke: #fff; stroke-width: 1.8; }
  .node:hover { stroke: #ffd54f; stroke-width: 3; }
  .edge { stroke: #5a657a; stroke-width: 2; fill: none; cursor: pointer; }
  .edge:hover { stroke: #ffd54f; stroke-width: 3; }
  .edge.thick { stroke-width: 3; }
  .edge-group, .node-group { transition: opacity 140ms ease; }
  .edge-group.is-dim, .node-group.is-dim { opacity: 0.15; }
  .edge-group.is-hidden, .node-group.is-hidden { display: none; }
  .edge-group.is-selected .edge,
  .edge-group.is-primary .edge {
    stroke: #ffd54f; stroke-width: 4;
    filter: drop-shadow(0 0 5px rgba(255, 213, 79, 0.45));
  }
  .edge-group.is-secondary .edge {
    stroke: #ffab91; stroke-width: 3.5;
    filter: drop-shadow(0 0 5px rgba(255, 171, 145, 0.32));
  }
  .edge-group.is-shared .edge {
    stroke: #80cbc4; stroke-width: 4.5;
    filter: drop-shadow(0 0 6px rgba(128, 203, 196, 0.38));
  }
  .edge-group.is-related .edge {
    stroke: #80cbc4;
    stroke-width: 2.5;
    stroke-dasharray: 10 8;
    opacity: 0.8;
    filter: none;
  }
  .edge-marker { fill: #2e3340; stroke: #5a657a; }
  .edge-group.is-selected .edge-marker,
  .edge-group.is-primary .edge-marker { fill: #423712; stroke: #ffd54f; }
  .edge-group.is-secondary .edge-marker { fill: #412922; stroke: #ffab91; }
  .edge-group.is-shared .edge-marker { fill: #173437; stroke: #80cbc4; }
  .edge-group.is-related .edge-marker {
    fill: rgba(33, 52, 51, 0.55);
    stroke: #80cbc4;
    stroke-dasharray: 4 3;
  }
  .edge-group.is-dim .edge-marker-text { opacity: 0.24; }
  .node-group.is-selected .node,
  .node-group.is-primary .node {
    stroke: #ffd54f; stroke-width: 4;
    filter: drop-shadow(0 0 6px rgba(255, 213, 79, 0.4));
  }
  .node-group.is-secondary .node {
    stroke: #ffab91; stroke-width: 3.5;
    filter: drop-shadow(0 0 5px rgba(255, 171, 145, 0.3));
  }
  .node-group.is-shared .node {
    stroke: #80cbc4; stroke-width: 4.5;
    filter: drop-shadow(0 0 6px rgba(128, 203, 196, 0.32));
  }
  .node-group.is-related .node {
    stroke: #80cbc4;
    stroke-width: 2.5;
    stroke-dasharray: 4 3;
    opacity: 0.88;
    filter: none;
  }
  text.label {
    font-size: 12px; fill: #d4d7e0;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    pointer-events: none;
  }
  text.tip {
    font-size: 11px; font-weight: bold; fill: #ffd54f;
    pointer-events: none;
  }
  text.lane-header {
    font-size: 12px; font-weight: bold; fill: #80cbc4;
    cursor: pointer;
  }
  text.lane-header.is-selected { fill: #ffd54f; }
  text.lane-header.is-primary { fill: #ffd54f; }
  text.lane-header.is-secondary { fill: #ffcab8; }
  text.lane-header.is-shared { fill: #d6fffb; }
  text.lane-header.is-related { fill: #9fd5cf; opacity: 0.82; }
  text.lane-header.is-dim { opacity: 0.3; }
  text.lane-header.is-hidden { visibility: hidden; }
  .node-group.is-selected text.label,
  .node-group.is-selected text.tip,
  .node-group.is-primary text.label,
  .node-group.is-primary text.tip { fill: #fff0b8; }
  .node-group.is-secondary text.label,
  .node-group.is-secondary text.tip { fill: #ffd9cf; }
  .node-group.is-shared text.label,
  .node-group.is-shared text.tip { fill: #d6fffb; }
  .node-group.is-related text.label,
  .node-group.is-related text.tip { fill: #9fd5cf; opacity: 0.82; }
  .node-group.is-dim text.label,
  .node-group.is-dim text.tip,
  .node-group.is-dim rect,
  .node-group.is-dim text.tag-text { opacity: 0.22; }
  .date-band.today .date-divider { stroke: #ff8a65; stroke-width: 2; }
  text.date-label.today {
    fill: #ffab91; font-weight: bold;
  }
  text.today-pill {
    font-size: 10px; font-weight: bold; fill: #ffd7c8;
    pointer-events: none;
  }
  text.date-label {
    font-size: 11px; fill: #6a7488;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    pointer-events: none;
  }
  h1 { font-size: 15px; margin: 0 0 8px; color: #ffd54f; }
  h2 { font-size: 11px; text-transform: uppercase; margin: 14px 0 4px;
       color: #80cbc4; letter-spacing: 0.5px; }
  .panel p, .panel div { font-size: 13px; line-height: 1.5; }
  .sha-mono { font-family: "SF Mono", Menlo, Consolas, monospace; }
  .commit-item {
    font-size: 12px; padding: 6px 8px; border-bottom: 1px solid #2e3340;
    line-height: 1.45;
  }
  .commit-item .sha { color: #ffd54f; font-family: "SF Mono", Menlo, monospace; }
  .commit-item .meta { color: #8891a4; font-size: 11px; }
  .badge {
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 10px; margin-right: 4px; background: #2e3340; color: #b0b6c4;
  }
  .badge.tip { background: #3b2f0d; color: #ffd54f; }
  .meta { color: #8891a4; font-size: 12px; }
  .empty { color: #7b8597; font-style: italic; }
  .branch-links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="graph" id="graph">
    <div class="toolbar">
      <div class="legend">
        <span><span class="dot" style="background:#ffd54f"></span>分支头</span>
        <span><span class="dot" style="background:#ef5350"></span>合并点</span>
        <span><span class="dot" style="background:#4fc3f7"></span>分叉点</span>
        <span><span class="dot" style="background:#9e9e9e"></span>普通</span>
        <span style="color:#ffab40">◌ 虚线描边 = 本地分支</span>
        <span><span style="background:#ffd54f;color:#2a1f00;padding:1px 5px;border-radius:3px;font-weight:bold;font-size:10px">tag</span> tag 徽章</span>
        <span><span style="background:#263238;color:#90caf9;padding:1px 5px;border-radius:3px;font-weight:bold;font-size:10px;border:1.5px dashed #90caf9">⬡ tag</span> 本地 tag（未推送）</span>
        <span style="color:#6a7488">┈ 水平虚线 = 日期时间线</span>
        <span class="today-note">今天: __TODAY__</span>
        <span class="hint">普通点击切换分支；Shift+点击进入对比；按住空白处拖动画布</span>
      </div>
      <div class="controls">
        <input id="branchSearch" type="text" placeholder="搜索分支，回车可选中当前第一项" />
        <button type="button" class="toggle-chip" id="filterToggle">只显示当前 + 关联</button>
        <div class="selection-status" id="selectionStatus">点击一个分支聚焦；Shift+点击第二个分支做对比</div>
      </div>
      <div class="branch-bar" id="branchBar"></div>
    </div>
    <svg id="svg"></svg>
  </div>
  <div class="panel" id="panel">
    <h1>使用说明</h1>
    <p>
      图中只显示<strong>分叉点</strong>、<strong>合并点</strong>、
      <strong>分支头</strong>和 <strong>tag</strong> 节点。每条连线代表
      两节点之间的线性提交链，点击连线可展开看其中所有提交。
    </p>
    <h2>快速阅读</h2>
    <div>
      · 每一列 (lane) = 一个分支，顶部绿字和上方分支胶囊都可点击<br>
      · 普通点击 = 切换主选分支；Shift+点击 = 加入/替换对比分支<br>
      · 从上到下 = 由新到旧<br>
      · 左侧灰色 <span class="sha-mono">YYYY-MM-DD</span> + 横向虚线 = 日期时间线<br>
      · 单分支模式：黄色实线 = 主选分支，青色虚线 = 关联分支，其他会变暗或被过滤<br>
      · 双分支模式：黄色 = 主选分支，橙色 = 对比分支，青色 = 两者共享关联<br>
      · 当天日期会高亮为 <span style="color:#ffab91">今天</span><br>
      · 可用上方搜索框快速定位分支；打开过滤模式可只看当前结果<br>
      · 节点旁文字 = <span class="sha-mono">short-sha</span> + subject<br>
      · 黄色徽章 = tag
    </div>
    <h2>脚本</h2>
    <div>
      <span class="sha-mono">python docs/git_graph/generate.py --help</span>
      查看所有 CLI 选项
    </div>
  </div>
</div>
<script>
const DATA = __DATA__;
const LAYOUT = __LAYOUT__;
const {LANE_W, ROW_H, MARGIN_X, MARGIN_Y, WIDTH, HEIGHT} = LAYOUT;

const SVGNS = "http://www.w3.org/2000/svg";
const graph = document.getElementById("graph");
const searchInput = document.getElementById("branchSearch");
const filterToggle = document.getElementById("filterToggle");
const selectionStatus = document.getElementById("selectionStatus");
const branchBar = document.getElementById("branchBar");
const svg   = document.getElementById("svg");
const panel = document.getElementById("panel");
const DEFAULT_PANEL_HTML = panel.innerHTML;

svg.setAttribute("width",  WIDTH);
svg.setAttribute("height", HEIGHT);

const nodeMap = {};
DATA.nodes.forEach(n => nodeMap[n.sha] = n);
const branchMeta = {};
DATA.branches.forEach(b => branchMeta[b.name] = b);
const laneHeaderEls = {};
const branchChipEls = {};
const nodeGroups = [];
const edgeGroups = [];
let selectedBranches = [];
let filterMode = false;

function pos(n) {
  return [MARGIN_X + n.lane * LANE_W + LANE_W / 2,
          MARGIN_Y + n.row  * ROW_H];
}

function el(name, attrs) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

function setStateClass(target, state) {
  target.classList.remove(
    "is-selected", "is-primary", "is-secondary",
    "is-shared", "is-related", "is-dim", "is-hidden",
  );
  if (state) target.classList.add(`is-${state}`);
}

function branchesOverlap(branches, names) {
  return (branches || []).some(name => names.has(name));
}

function orderedBranchNames(names) {
  const wanted = new Set(names);
  return DATA.branches.map(b => b.name).filter(name => wanted.has(name));
}

function relatedBranchesOf(branch) {
  const related = new Set();
  [...DATA.nodes, ...DATA.segments].forEach(item => {
    const branches = item.branches || [];
    if (!branches.includes(branch) || branches.length <= 1) return;
    branches.forEach(name => {
      if (name !== branch) related.add(name);
    });
  });
  return orderedBranchNames(related);
}

function selectionInfo() {
  const primary = selectedBranches[0] || null;
  const secondary = selectedBranches[1] || null;
  const related = primary && !secondary ? new Set(relatedBranchesOf(primary)) : new Set();
  return { primary, secondary, related };
}

function branchButtonState(name, info = selectionInfo()) {
  if (!info.primary) return null;
  if (name === info.primary) return "primary";
  if (name === info.secondary) return "secondary";
  if (!info.secondary && info.related.has(name)) return "related";
  return "dim";
}

function laneHeaderText(name, state) {
  if (state === "primary") return `★ ${name}`;
  if (state === "secondary") return `◆ ${name}`;
  return name;
}

function branchButtonHtml(name, cls = "branch-link", forcedState = null) {
  const meta = branchMeta[name];
  const scope = meta && meta.is_local ? "local" : "remote";
  const color = meta ? meta.color : "#8891a4";
  const state = forcedState || branchButtonState(name);
  const stateText = {
    primary: "主选",
    secondary: "对比",
    shared: "共享",
    related: "关联",
  }[state];
  const extra = stateText ? `${scope} · ${stateText}` : scope;
  const classes = state ? `${cls} is-${state}` : cls;
  return `<button type="button" class="${classes}" data-branch="${esc(name)}">
    <span class="branch-swatch" style="background:${esc(color)}"></span>${esc(name)}
    <span class="branch-scope">${extra}</span>
  </button>`;
}

function showDefaultPanel() {
  panel.innerHTML = DEFAULT_PANEL_HTML;
}

function showBranch(name) {
  const related = relatedBranchesOf(name);
  const nodes = DATA.nodes.filter(n => (n.branches || []).includes(name));
  const segments = DATA.segments.filter(s => (s.branches || []).includes(name));
  const tips = DATA.nodes.filter(n => (n.tip_names || []).includes(name));
  const dates = nodes.map(n => n.date).sort().reverse();
  const newest = dates[0] || "—";
  const oldest = dates[dates.length - 1] || "—";

  let h = `<h1>${esc(name)}</h1>`;
  h += `<div>${branchButtonHtml(name, "branch-link", "primary")}</div>`;
  h += `<h2>可视范围</h2>`;
  h += `<div class="meta">节点 ${nodes.length} 个 · 折叠链路 ${segments.length} 段</div>`;
  h += `<div class="meta">时间窗口 ${esc(newest)} → ${esc(oldest)}</div>`;
  h += `<h2>关联分支</h2>`;
  if (related.length) {
    h += `<div class="branch-links">${related.map(other => branchButtonHtml(other, "branch-link", "related")).join("")}</div>`;
    h += `<div class="meta">关联定义：在当前简化图里与该分支共享可视节点或连线。</div>`;
  } else {
    h += `<div class="empty">当前窗口内没有检测到共享节点/连线的其它分支。</div>`;
  }
  h += `<h2>分支头</h2>`;
  if (tips.length) {
    h += tips.map(tip => `
      <div class="commit-item">
        <span class="sha">${esc(tip.short)}</span>
        <span class="meta">  ${esc(tip.date)} · ${esc(tip.author)}</span><br>
        ${esc(tip.subject)}
      </div>
    `).join("");
  } else {
    h += `<div class="empty">当前窗口内没有保留到该分支的 tip 节点。</div>`;
  }
  panel.innerHTML = h;
}

function showBranchPair(primary, secondary) {
  const sharedNodes = DATA.nodes.filter(n => {
    const branches = n.branches || [];
    return branches.includes(primary) && branches.includes(secondary);
  });
  const sharedSegments = DATA.segments.filter(s => {
    const branches = s.branches || [];
    return branches.includes(primary) && branches.includes(secondary);
  });
  const mergeNodes = sharedNodes.filter(n => n.kind === "merge");
  const splitNodes = sharedNodes.filter(n => n.kind === "split");
  const latestShared = sharedNodes.slice(0, 6);

  let h = `<h1>${esc(primary)} ⇄ ${esc(secondary)}</h1>`;
  h += `<div class="branch-links">`;
  h += branchButtonHtml(primary, "branch-link", "primary");
  h += branchButtonHtml(secondary, "branch-link", "secondary");
  h += `</div>`;
  h += `<h2>共同关联</h2>`;
  h += `<div class="meta">共享节点 ${sharedNodes.length} 个 · 共享链路 ${sharedSegments.length} 段</div>`;
  h += `<div class="meta">合并点 ${mergeNodes.length} 个 · 分叉点 ${splitNodes.length} 个</div>`;

  h += `<h2>重点看这里</h2>`;
  if (mergeNodes.length) {
    h += `<div class="meta">下面这些 merge 节点同时属于这两个分支，最适合观察它们的合并关系。</div>`;
    h += mergeNodes.map(commitBlock).join("");
  } else if (latestShared.length) {
    h += `<div class="meta">当前窗口里没有直接共享的 merge 节点，先看它们共享的可视节点。</div>`;
    h += latestShared.map(commitBlock).join("");
  } else {
    h += `<div class="empty">当前时间窗口内没有找到这两个分支的共同可视节点；可尝试扩大 --days。</div>`;
  }

  panel.innerHTML = h;
}

// --- timeline: 按日期分隔的水平虚线 + 左侧日期标签 --------------------------
// 每个日期独占一片时间带；线画在"每个日期组的最上一行上方"，这样相邻日期间恰好有一条分隔。
const nodesByDate = {};
DATA.nodes.forEach(n => {
  (nodesByDate[n.date] ||= []).push(n.row);
});
Object.entries(nodesByDate).forEach(([date, rowList]) => {
  const topRow = Math.min(...rowList);
  const y = MARGIN_Y + topRow * ROW_H - ROW_H / 2;
  const isToday = date === DATA.today;
  const band = el("g", {class: `date-band${isToday ? " today" : ""}`});

  // 横向虚线（不伸进日期标签区域）
  const line = el("line", {
    x1: MARGIN_X - 8, y1: y,
    x2: WIDTH - 20,   y2: y,
    class: "date-divider",
    stroke: isToday ? "#ff8a65" : "#3a4150",
    "stroke-dasharray": "5 6",
    "stroke-width": isToday ? 2 : 1,
  });
  band.appendChild(line);

  // 日期标签（左侧，在虚线正上方）
  const lbl = el("text", {
    x: 10, y: y - 4,
    class: `date-label${isToday ? " today" : ""}`,
  });
  lbl.textContent = isToday ? `${date} · 今天` : date;
  band.appendChild(lbl);

  if (isToday) {
    const badgeX = MARGIN_X - 54;
    const badge = el("rect", {
      x: badgeX, y: y - 18,
      width: 44, height: 16, rx: 8, ry: 8,
      fill: "#ff8a65", opacity: 0.24,
    });
    band.appendChild(badge);
    const badgeText = el("text", {
      x: badgeX + 22, y: y - 7,
      class: "today-pill", "text-anchor": "middle",
    });
    badgeText.textContent = "TODAY";
    band.appendChild(badgeText);
  }

  svg.appendChild(band);
});

// --- lane headers (跳过空列) ------------------------------------------------
const usedLanes = new Set(DATA.nodes.map(n => n.lane));
DATA.lane_labels.forEach((name, i) => {
  if (!usedLanes.has(i)) return;
  const x = MARGIN_X + i * LANE_W + LANE_W / 2;
  const t = el("text", {x, y: 30, class: "lane-header", "text-anchor": "middle"});
  t.textContent = name;
  t.addEventListener("click", (evt) => selectBranch(name, {compare: evt.shiftKey}));
  laneHeaderEls[name] = t;
  svg.appendChild(t);
});

// --- branch pills -----------------------------------------------------------
function renderBranchBar() {
  const chips = DATA.branches.map(branch => branchButtonHtml(branch.name, "branch-chip")).join("");
  branchBar.innerHTML = `
    <button type="button" class="clear-chip" data-clear="1">清除选择</button>
    ${chips}
  `;
  branchBar.querySelectorAll("[data-branch]").forEach(btn => {
    branchChipEls[btn.dataset.branch] = btn;
  });
}

branchBar.addEventListener("click", (evt) => {
  const clearBtn = evt.target.closest("[data-clear]");
  if (clearBtn) {
    selectedBranches = [];
    updateBranchSelection();
    showDefaultPanel();
    return;
  }
  const btn = evt.target.closest("[data-branch]");
  if (btn) selectBranch(btn.dataset.branch, {compare: evt.shiftKey});
});

searchInput.addEventListener("input", () => {
  applyBranchSearch();
});

searchInput.addEventListener("keydown", (evt) => {
  if (evt.key === "Escape") {
    searchInput.value = "";
    applyBranchSearch();
    return;
  }
  if (evt.key !== "Enter") return;
  const firstVisible = [...branchBar.querySelectorAll(".branch-chip")]
    .find(btn => btn.style.display !== "none");
  if (firstVisible) selectBranch(firstVisible.dataset.branch);
});

filterToggle.addEventListener("click", () => {
  filterMode = !filterMode;
  updateBranchSelection();
  refreshPanel();
});

// --- edges ------------------------------------------------------------------
DATA.segments.forEach(seg => {
  const a = nodeMap[seg.child], b = nodeMap[seg.parent];
  if (!a || !b) return;
  const [ax, ay] = pos(a), [bx, by] = pos(b);
  const group = el("g", {class: "edge-group"});

  let d;
  if (ax === bx) {
    d = `M ${ax} ${ay} L ${bx} ${by}`;
  } else {
    const midY = (ay + by) / 2;
    d = `M ${ax} ${ay} C ${ax} ${midY}, ${bx} ${midY}, ${bx} ${by}`;
  }
  const p = el("path", {d, class: "edge" + (seg.hidden.length > 3 ? " thick" : "")});
  group.appendChild(p);

  if (seg.hidden.length > 0) {
    const mx = (ax + bx) / 2, my = (ay + by) / 2;
    const bg = el("circle", {cx: mx, cy: my, r: 9, class: "edge-marker"});
    group.appendChild(bg);
    const t = el("text", {
      x: mx, y: my + 4, "text-anchor": "middle",
      class: "edge-marker-text",
      "font-size": 10, fill: "#d4d7e0",
    });
    t.style.pointerEvents = "none";
    t.textContent = seg.hidden.length;
    group.appendChild(t);
  }
  group.addEventListener("click", () => showSegment(seg));
  edgeGroups.push({seg, group});
  svg.appendChild(group);
});

// --- nodes ------------------------------------------------------------------
const FILL = {
  tip:   "#ffd54f",
  merge: "#ef5350",
  split: "#4fc3f7",
  tag:   "#ba68c8",
  node:  "#9e9e9e",
};

DATA.nodes.forEach(n => {
  const [x, y] = pos(n);
  const color = FILL[n.kind] || "#9e9e9e";
  const group = el("g", {class: "node-group"});

  let shape;
  if (n.kind === "merge") {
    const r = 9;
    shape = el("polygon", {
      points: `${x},${y-r} ${x+r},${y} ${x},${y+r} ${x-r},${y}`,
      fill: color, class: "node",
    });
  } else if (n.kind === "split") {
    const r = 7;
    shape = el("rect", {
      x: x - r, y: y - r, width: 2 * r, height: 2 * r,
      fill: color, class: "node",
    });
  } else {
    const r = n.kind === "tip" ? 10 : 7;
    shape = el("circle", {cx: x, cy: y, r, fill: color, class: "node"});
  }
  if (n.is_local) {
    shape.setAttribute("stroke-dasharray", "3 2");
    shape.setAttribute("stroke", "#ffab40");
    shape.setAttribute("stroke-width", "2.5");
  }
  shape.addEventListener("click", () => showNode(n));
  group.appendChild(shape);

  // 右侧标签区：tag 徽章 + short-sha + subject
  const labelX = x + 15;
  let cursorX = labelX;

  (n.tag_names || []).forEach(tag => {
    const padding = 5;
    const approxWidth = tag.length * 6.5 + padding * 2;
    const tagY = y - 9;
    const rect = el("rect", {
      x: cursorX, y: tagY,
      width: approxWidth, height: 18,
      rx: 3, ry: 3,
      fill: "#ffd54f", stroke: "#8d6e2a", "stroke-width": 1,
    });
    group.appendChild(rect);
    const tagText = el("text", {
      x: cursorX + padding, y: tagY + 13,
      class: "tag-text",
      "font-size": 11, "font-weight": "bold",
      fill: "#2a1f00", "font-family": "SF Mono, Menlo, monospace",
    });
    tagText.style.pointerEvents = "none";
    tagText.textContent = tag;
    group.appendChild(tagText);
    cursorX += approxWidth + 4;
  });

  (n.local_tag_names || []).forEach(tag => {
    const padding = 5;
    const label = "⬡ " + tag;
    const approxWidth = label.length * 6.5 + padding * 2;
    const tagY = y - 9;
    const rect = el("rect", {
      x: cursorX, y: tagY,
      width: approxWidth, height: 18,
      rx: 3, ry: 3,
      fill: "#263238", stroke: "#90caf9", "stroke-width": 1.5,
      "stroke-dasharray": "3,2",
    });
    group.appendChild(rect);
    const tagText = el("text", {
      x: cursorX + padding, y: tagY + 13,
      class: "tag-text",
      "font-size": 11, "font-weight": "bold",
      fill: "#90caf9", "font-family": "SF Mono, Menlo, monospace",
    });
    tagText.style.pointerEvents = "none";
    tagText.textContent = label;
    group.appendChild(tagText);
    cursorX += approxWidth + 4;
  });

  const lbl = el("text", {x: cursorX, y: y + 4, class: "label"});
  const maxLen = ((n.tag_names || []).length || (n.local_tag_names || []).length) ? 36 : 50;
  lbl.textContent = `${n.short}  ${n.subject.slice(0, maxLen)}${n.subject.length > maxLen ? "…" : ""}`;
  group.appendChild(lbl);

  // 节点上方的分支名标签（本地用橙色 + [local] 后缀）
  if (n.tip_names.length) {
    const t = el("text", {
      x, y: y - 22, class: "tip", "text-anchor": "middle",
    });
    if (n.is_local) {
      t.setAttribute("fill", "#ffab40");
      t.textContent = n.tip_names.map(nm => nm + " [local]").join(" · ");
    } else {
      t.textContent = n.tip_names.join(" · ");
    }
    group.appendChild(t);
  }

  nodeGroups.push({node: n, group});
  svg.appendChild(group);
});

// --- panel rendering --------------------------------------------------------
function commitBlock(c) {
  return `<div class="commit-item">
    <span class="sha">${esc(c.short)}</span>
    <span class="meta">  ${esc(c.date)} · ${esc(c.author)}</span><br>
    ${esc(c.subject)}
    ${c.refs ? `<br><span class="meta">${esc(c.refs)}</span>` : ""}
  </div>`;
}

function showNode(n) {
  const kindLabel = {
    tip: "分支头", merge: "合并点", split: "分叉点",
    tag: "Tag", node: "节点",
  }[n.kind] || n.kind;
  let h = `<h1>${esc(n.short)} · ${kindLabel}</h1>`;
  if ((n.branches || []).length) {
    h += `<div class="branch-links">${n.branches.map(name => branchButtonHtml(name, "branch-link")).join("")}</div>`;
  }
  if (n.tip_names.length) {
    h += `<div>${n.tip_names.map(s => `<span class="badge tip">${esc(s)}</span>`).join("")}</div>`;
  }
  h += `<h2>Subject</h2><div>${esc(n.subject)}</div>`;
  h += `<h2>Author / Date</h2><div>${esc(n.author)} · ${esc(n.date)}</div>`;
  if (n.refs) h += `<h2>Refs</h2><div>${esc(n.refs)}</div>`;
  h += `<h2>Full SHA</h2><div class="sha-mono">${esc(n.sha)}</div>`;
  panel.innerHTML = h;
}

function showSegment(seg) {
  const a = nodeMap[seg.child], b = nodeMap[seg.parent];
  let h = `<h1>${esc(a.short)} → ${esc(b.short)}</h1>`;
  if ((seg.branches || []).length) {
    h += `<div class="branch-links">${seg.branches.map(name => branchButtonHtml(name, "branch-link")).join("")}</div>`;
  }
  h += `<div class="meta">此段包含 ${seg.hidden.length} 个折叠的线性提交</div>`;
  if (seg.hidden.length === 0) {
    h += `<p>(直接父子关系，无折叠提交)</p>`;
  } else {
    h += `<h2>Commits (新 → 旧)</h2>`;
    h += seg.hidden.map(commitBlock).join("");
  }
  panel.innerHTML = h;
}

panel.addEventListener("click", (evt) => {
  const btn = evt.target.closest("[data-branch]");
  if (btn) selectBranch(btn.dataset.branch, {compare: evt.shiftKey});
});

function selectionStateForBranches(branches, info) {
  if (!info.primary) return null;
  const names = branches || [];
  const hasPrimary = names.includes(info.primary);
  const hasSecondary = info.secondary && names.includes(info.secondary);

  if (info.secondary) {
    if (hasPrimary && hasSecondary) return "shared";
    if (hasPrimary) return "primary";
    if (hasSecondary) return "secondary";
    return filterMode ? "hidden" : "dim";
  }

  if (hasPrimary) return "primary";
  if (branchesOverlap(names, info.related)) return "related";
  return filterMode ? "hidden" : "dim";
}

function refreshPanel() {
  const info = selectionInfo();
  if (info.primary && info.secondary) {
    showBranchPair(info.primary, info.secondary);
  } else if (info.primary) {
    showBranch(info.primary);
  } else {
    showDefaultPanel();
  }
}

function updateSelectionStatus() {
  const info = selectionInfo();
  filterToggle.classList.toggle("is-on", filterMode);
  filterToggle.textContent = filterMode ? "只显示当前 + 关联：开" : "只显示当前 + 关联";

  if (!info.primary) {
    selectionStatus.textContent = "点击一个分支聚焦；Shift+点击第二个分支做对比";
    return;
  }
  if (!info.secondary) {
    selectionStatus.textContent = `主选：${info.primary}；普通点击会切换，Shift+点击可进入双分支对比`;
    return;
  }
  selectionStatus.textContent = `主选：${info.primary}；对比：${info.secondary}；普通点击会切换主选，Shift+点击会替换对比分支`;
}

function applyBranchSearch() {
  const query = searchInput.value.trim().toLowerCase();
  Object.entries(branchChipEls).forEach(([name, btn]) => {
    const keepVisible = selectedBranches.includes(name);
    btn.style.display = !query || keepVisible || name.toLowerCase().includes(query) ? "" : "none";
  });
}

function updateBranchSelection() {
  const info = selectionInfo();

  nodeGroups.forEach(({node, group}) => {
    setStateClass(group, selectionStateForBranches(node.branches, info));
  });

  edgeGroups.forEach(({seg, group}) => {
    setStateClass(group, selectionStateForBranches(seg.branches, info));
  });

  DATA.branches.forEach(branch => {
    const state = branchButtonState(branch.name, info);
    const headerState = filterMode && info.primary && state === "dim" ? "hidden" : state;

    if (laneHeaderEls[branch.name]) {
      laneHeaderEls[branch.name].textContent = laneHeaderText(branch.name, state);
      setStateClass(laneHeaderEls[branch.name], headerState);
    }
    if (branchChipEls[branch.name]) {
      setStateClass(branchChipEls[branch.name], state);
    }
  });

  const clearBtn = branchBar.querySelector("[data-clear]");
  if (clearBtn) setStateClass(clearBtn, selectedBranches.length ? null : "dim");

  updateSelectionStatus();
  applyBranchSearch();
}

function selectBranch(name, options = {}) {
  const compare = !!options.compare;
  const [primary, secondary] = selectedBranches;

  if (!compare) {
    if (primary === name && !secondary) {
      selectedBranches = [];
    } else {
      selectedBranches = [name];
    }
  } else if (!primary) {
    selectedBranches = [name];
  } else if (name === primary && secondary) {
    selectedBranches = [primary];
  } else if (name === secondary) {
    selectedBranches = [primary];
  } else if (name === primary) {
    selectedBranches = [primary];
  } else {
    selectedBranches = [primary, name];
  }

  updateBranchSelection();
  refreshPanel();
}

// --- drag to pan ------------------------------------------------------------
let dragState = null;

graph.addEventListener("mousedown", (evt) => {
  if (evt.button !== 0) return;
  if (evt.target.closest(".toolbar, .lane-header, .node-group, .edge-group")) return;
  dragState = {
    x: evt.clientX,
    y: evt.clientY,
    left: graph.scrollLeft,
    top: graph.scrollTop,
  };
  graph.classList.add("dragging");
  evt.preventDefault();
});

window.addEventListener("mousemove", (evt) => {
  if (!dragState) return;
  graph.scrollLeft = dragState.left - (evt.clientX - dragState.x);
  graph.scrollTop = dragState.top - (evt.clientY - dragState.y);
});

window.addEventListener("mouseup", () => {
  dragState = null;
  graph.classList.remove("dragging");
});

renderBranchBar();
updateBranchSelection();
showDefaultPanel();
</script>
</body>
</html>
"""


def render_html(
    nodes: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    cfg: Config,
) -> str:
    lane_w, row_h = 200, 54
    margin_x, margin_y = 92, 70
    max_lane = max((n["lane"] for n in nodes), default=0) + 1
    max_row  = max((n["row"]  for n in nodes), default=0) + 1
    width  = margin_x * 2 + max_lane * lane_w + 320
    height = margin_y * 2 + max_row  * row_h

    layout = {
        "LANE_W": lane_w, "ROW_H": row_h,
        "MARGIN_X": margin_x, "MARGIN_Y": margin_y,
        "WIDTH": width, "HEIGHT": height,
    }
    today = datetime.now().strftime("%Y-%m-%d")
    data = {
        "nodes": nodes,
        "segments": segments,
        "lane_labels": [name for name, _ in cfg.branches],
        "today": today,
        "branches": [
            {
                "name": name,
                "color": color,
                "is_local": not is_remote_ref(name, cfg.remotes),
            }
            for name, color in cfg.branches
        ],
    }

    return (HTML_TEMPLATE
            .replace("__DATA__",   json.dumps(data, ensure_ascii=False))
            .replace("__LAYOUT__", json.dumps(layout))
            .replace("__TODAY__", today))


# -----------------------------------------------------------------------------#
# 主入口
# -----------------------------------------------------------------------------#

def _get_commit_ts(sha: str, repo: Path) -> int:
    """返回 SHA（commit 或 annotated tag object）对应 commit 的 unix 时间戳。"""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", sha + "^{commit}"],
            check=True, text=True, capture_output=True, cwd=str(repo),
        )
        ts = result.stdout.strip()
        return int(ts) if ts else 0
    except (subprocess.CalledProcessError, ValueError):
        return 0


def _get_tag_ts(sha: str, repo: Path) -> int:
    """返回 tag 被打出的时间戳。

    Annotated tag 有独立的 taggerdate（即 git tag -a 时的时刻），优先使用。
    Lightweight tag 没有 tag object，退回到 commit 时间戳。
    """
    try:
        obj_type = subprocess.run(
            ["git", "cat-file", "-t", sha],
            check=True, text=True, capture_output=True, cwd=str(repo),
        ).stdout.strip()
        if obj_type == "tag":
            content = subprocess.run(
                ["git", "cat-file", "-p", sha],
                check=True, text=True, capture_output=True, cwd=str(repo),
            ).stdout
            for line in content.split("\n"):
                if line.startswith("tagger "):
                    # "tagger Name <email> TIMESTAMP TIMEZONE"
                    parts = line.split()
                    return int(parts[-2])
    except (subprocess.CalledProcessError, ValueError, IndexError):
        pass
    return _get_commit_ts(sha, repo)


def _sync_tags(repo: Path) -> set:
    """与远端同步 tag，返回本地独有（未推送）的 tag 名集合。

    本地独有 tag → 不删除，原样保留，返回其名称供 HTML 标记
    两边都有但 SHA 不同：
      - 比较 tag 打出的时间（annotated tag 用 taggerdate，lightweight 用 commit date）
      - 远端 tag 更新 → 以远端为准（force 更新本地）
      - 本地 tag 更新 → 保留本地
    """
    remotes = get_remotes(repo)

    # 远端 tag → SHA
    remote_tag_sha: dict[str, str] = {}
    for remote in remotes:
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--tags", "--refs", remote],
                check=True, text=True, capture_output=True, cwd=str(repo),
            )
            for line in result.stdout.split("\n"):
                if "\t" not in line:
                    continue
                sha, ref = line.split("\t", 1)
                prefix = "refs/tags/"
                tag = ref[len(prefix):] if ref.startswith(prefix) else ref
                remote_tag_sha[tag] = sha.strip()
        except subprocess.CalledProcessError:
            pass

    # 本地 tag → SHA
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "--format=%(refname:short)\t%(objectname)"],
            check=True, text=True, capture_output=True, cwd=str(repo),
        )
        local_tag_sha: dict[str, str] = {}
        for line in result.stdout.split("\n"):
            if "\t" not in line:
                continue
            tag, sha = line.split("\t", 1)
            local_tag_sha[tag] = sha.strip()
    except subprocess.CalledProcessError:
        return

    local_only: list[str] = []
    to_update:  list[str] = []
    to_keep:    list[str] = []

    # 找出本地与远端 SHA 不同的 tag，临时拉取远端 tag object 以便比较 taggerdate
    conflicting = {
        tag: remote_tag_sha[tag]
        for tag in local_tag_sha
        if tag in remote_tag_sha and remote_tag_sha[tag] != local_tag_sha[tag]
    }
    fetched_tmp_refs: list[str] = []
    for tag, remote_sha in conflicting.items():
        try:
            subprocess.run(
                ["git", "fetch", "origin",
                 f"refs/tags/{tag}:refs/tmp_remote_tags/{tag}"],
                check=True, text=True, capture_output=True, cwd=str(repo),
            )
            fetched_tmp_refs.append(f"refs/tmp_remote_tags/{tag}")
        except subprocess.CalledProcessError:
            pass  # 拉不到就退回 commit 时间戳比较

    for tag in sorted(local_tag_sha):
        if tag not in remote_tag_sha:
            local_only.append(tag)                  # 本地独有，不删除
        elif remote_tag_sha[tag] != local_tag_sha[tag]:
            local_ts  = _get_tag_ts(local_tag_sha[tag], repo)
            remote_ts = _get_tag_ts(remote_tag_sha[tag], repo)
            if remote_ts > local_ts:
                to_update.append(tag)               # 远端 tag 更新 → 覆盖本地
            else:
                to_keep.append(tag)                 # 本地 tag 更新 → 保留

    # 清理临时拉取的 remote tag refs
    for ref in fetched_tmp_refs:
        try:
            subprocess.run(
                ["git", "update-ref", "-d", ref],
                check=True, capture_output=True, cwd=str(repo),
            )
        except subprocess.CalledProcessError:
            pass

    if local_only:
        print(f"Tag sync: {len(local_only)} local-only tag(s) (not on remote, kept):")
        for tag in local_only:
            print(f"  local-only: {tag}")

    if to_update:
        print(f"Tag sync: updating {len(to_update)} tag(s) moved on remote…")
        for tag in to_update:
            try:
                subprocess.run(
                    ["git", "tag", "-f", tag, remote_tag_sha[tag]],
                    check=True, text=True, capture_output=True, cwd=str(repo),
                )
                print(f"  updated: {tag}  ({local_tag_sha[tag][:8]} → {remote_tag_sha[tag][:8]})")
            except subprocess.CalledProcessError:
                print(f"  [warn] could not update: {tag}")

    if to_keep:
        print(f"Tag sync: keeping {len(to_keep)} local tag(s) newer than remote:")
        for tag in to_keep:
            print(f"  kept (local newer): {tag}")

    return set(local_only)


def fetch_remotes(repo: Path) -> set:
    """绘图前同步远端分支，对齐 tag，返回本地独有 tag 名集合。"""
    print("Fetching remotes (git fetch --all --prune)…")
    try:
        out = subprocess.run(
            ["git", "fetch", "--all", "--prune"],
            check=True, text=True, capture_output=True, cwd=str(repo),
        )
        if out.stderr.strip():
            print(out.stderr.strip())
    except subprocess.CalledProcessError as e:
        print(f"[warn] git fetch exited with {e.returncode}, 继续使用本地状态")
        if e.stderr:
            print(e.stderr.rstrip())

    return _sync_tags(repo)


def open_output(output: Path) -> None:
    uri = output.resolve().as_uri()
    print(f"Opening        : {output}")
    if not webbrowser.open_new_tab(uri):
        print(f"[warn] 无法自动打开浏览器，请手动打开: {output}")


def build_config(args: argparse.Namespace) -> Config:
    try:
        repo = Path(git("rev-parse", "--show-toplevel", repo=args.repo)).resolve()
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"目标路径不是 git 仓库: {args.repo}") from e

    remotes = get_remotes(repo)

    if args.branches:
        branches_raw = args.branches
        print(f"Using {len(branches_raw)} branches from --branches")
    else:
        branches_raw = discover_active_branches(
            active_days=args.active_days,
            include_local=args.include_local,
            remotes=remotes,
            max_branches=args.max_branches,
            repo=repo,
        )
        if not branches_raw:
            raise SystemExit(
                f"自动发现未找到任何最近 {args.active_days} 天活跃的分支。"
                f"试试 --active-days 更大的值，或用 --branches 显式指定。"
            )

    branches = assign_colors(branches_raw, remotes)
    print(f"Repo           : {repo}")
    print_branch_summary(branches, remotes, repo)

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    output = args.output.resolve() if args.output else repo / "docs" / "git_graph" / "index.html"
    return Config(
        repo=repo,
        branches=branches,
        remotes=remotes,
        since=since,
        output=output,
        fetch=not args.no_fetch,
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    local_only_tags: set = set()
    if not args.no_fetch:
        try:
            repo = Path(git("rev-parse", "--show-toplevel", repo=args.repo)).resolve()
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"目标路径不是 git 仓库: {args.repo}") from e
        local_only_tags = fetch_remotes(repo)

    cfg = build_config(args)
    cfg.local_only_tags = local_only_tags
    print(f"Window: commits since {cfg.since}")

    commits  = load_commits(cfg)
    tips, colors = resolve_tips(commits, cfg)
    keep     = select_interesting(commits, tips)
    reach    = compute_reach_branches(commits, keep, tips)
    segments = walk_segments(commits, keep, reach, cfg)
    lanes    = compute_lanes(reach, cfg)
    rows     = compute_rows(commits, keep)
    nodes    = build_nodes(commits, keep, tips, colors, reach, lanes, rows, cfg)

    cfg.output.parent.mkdir(parents=True, exist_ok=True)
    cfg.output.write_text(render_html(nodes, segments, cfg), encoding="utf-8")

    print()
    print(f"Commits loaded : {len(commits)}")
    print(f"Nodes kept     : {len(nodes)}")
    print(f"Segments       : {len(segments)}")
    print(f"Output         : {cfg.output}")

    if args.open:
        open_output(cfg.output)


if __name__ == "__main__":
    main()
