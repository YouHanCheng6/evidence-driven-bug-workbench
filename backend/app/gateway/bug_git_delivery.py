"""Publish and finally submit one verified Bug repair through one Git policy."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.gateway.bug_rollback import (
    REPAIR_FILE_LOCK,
    _baseline_git_roots,
    _git_optional,
    _porcelain_paths,
    _repair_snapshot_files,
    _repo_path,
)
from deerflow.config import get_app_config


class BugGitDeliveryError(RuntimeError):
    """The requested delivery cannot be completed without human intervention."""


def _require_git_delivery_enabled() -> None:
    """Public-example safeguard, including operations on historical records."""
    config = get_app_config().bug_analysis.git_relay
    if not config.enabled or not str(config.remote_url or "").strip():
        raise BugGitDeliveryError("Git 交付未启用或未配置；未读取或修改源码仓库")


@dataclass(frozen=True)
class BugRepairGitContext:
    bug_id: int
    workflow_id: str
    title: str
    repo: Path
    paths: tuple[str, ...]
    branch: str
    official_remote: str
    official_remote_url: str
    official_branch: str
    base_commit: str
    subject: str


def _run(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        env=env,
    )


def _message(completed: subprocess.CompletedProcess[bytes]) -> str:
    value = completed.stderr or completed.stdout
    return value.decode("utf-8", "replace").strip()


def _formal_submit_env(base: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **(base or os.environ),
        "GIT_AUTHOR_NAME": "example_user",
        "GIT_AUTHOR_EMAIL": "example@example.invalid",
        "GIT_COMMITTER_NAME": "example_user",
        "GIT_COMMITTER_EMAIL": "example@example.invalid",
    }


def _delivery_paths(snapshot: dict[str, Any]) -> tuple[Path, list[str]]:
    files = _repair_snapshot_files(snapshot)
    roots = _baseline_git_roots(snapshot)
    if len(roots) != 1:
        raise BugGitDeliveryError("本次修改没有唯一的 Git 仓库，未提交")
    root = roots[0]
    repo_root = Path(str(root.get("repo_root") or "")).expanduser().resolve()
    prefix = str(root.get("path_prefix") or "")
    if not (repo_root / ".git").exists():
        raise BugGitDeliveryError("本次修改对应的 Git 仓库已不存在，未提交")
    paths: list[str] = []
    for entry, path in files:
        expected = entry.get("after_sha256")
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        if expected != actual or (expected is None and entry.get("after_exists", True)):
            raise BugGitDeliveryError(f"文件已在修复后被再次修改，未提交：{entry.get('path')}")
        paths.append(_repo_path(prefix, str(entry["path"])).as_posix())
    if not paths:
        raise BugGitDeliveryError("本次任务没有可提交的代码修改")
    return repo_root, sorted(dict.fromkeys(paths))


def _tracking(repo: Path) -> tuple[str, str, str]:
    branch = _git_optional(repo, "symbolic-ref", "--short", "HEAD")
    if not branch:
        raise BugGitDeliveryError("仓库当前不在分支上，未提交")
    remote = _git_optional(repo, "config", "--get", f"branch.{branch}.remote")
    merge_ref = _git_optional(repo, "config", "--get", f"branch.{branch}.merge")
    if not remote or remote == "." or not merge_ref or not merge_ref.startswith("refs/heads/"):
        raise BugGitDeliveryError(f"当前分支 {branch} 没有远程跟踪分支，未提交")
    remote_branch = merge_ref.removeprefix("refs/heads/")
    if _git_optional(repo, "remote", "get-url", remote) is None:
        raise BugGitDeliveryError(f"当前分支的远程 {remote} 不可用，未提交")
    return branch, remote, remote_branch


def _repair_context(workflow: dict[str, Any], *, require_exclusive: bool = True) -> BugRepairGitContext:
    """Resolve and validate the sole source repository for one Bug snapshot."""
    bug_id = int(workflow["bug_id"])
    snapshot = workflow.get("rollback")
    bug_snapshot = workflow.get("bug_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        raise BugGitDeliveryError("本次任务没有可交付的代码修改快照")
    if snapshot.get("completed"):
        raise BugGitDeliveryError("本次修改当前处于已回退状态，请先恢复再继续")
    if not isinstance(bug_snapshot, dict) or not str(bug_snapshot.get("title") or "").strip():
        raise BugGitDeliveryError("本次任务缺少禅道完整标题，未交付")

    roots = _baseline_git_roots(snapshot)
    if len(roots) != 1:
        raise BugGitDeliveryError("本次修改没有唯一的 Git 仓库，未交付")
    root = roots[0]
    repo, paths = _delivery_paths(snapshot)
    branch, remote, remote_branch = _tracking(repo)
    base_commit = str(root.get("head") or "").strip()
    recorded_branch = str(root.get("branch") or "").strip()
    if not base_commit or _git_optional(repo, "cat-file", "-t", base_commit) != "commit":
        raise BugGitDeliveryError("本次修改的 Git 基准提交已不存在，未交付")
    if recorded_branch and recorded_branch != branch:
        raise BugGitDeliveryError(f"仓库分支已从 {recorded_branch} 切换为 {branch}，未交付")
    if _git_optional(repo, "rev-parse", "HEAD") != base_commit:
        raise BugGitDeliveryError("本次修改后仓库基准提交已变化，未交付")
    official_remote_url = _git_optional(repo, "remote", "get-url", remote)
    if not official_remote_url:
        raise BugGitDeliveryError(f"公司远程 {remote} 不可用，未交付")

    dirty = _porcelain_paths(repo)
    preexisting = {_repo_path(str(root.get("path_prefix") or ""), str(path)).as_posix() for path in root.get("dirty_before", []) if isinstance(path, str)}
    unexpected = sorted(dirty - set(paths) - (set() if require_exclusive else preexisting))
    missing = sorted(set(paths) - dirty)
    if unexpected:
        raise BugGitDeliveryError("仓库还有不属于本 Bug 的未提交修改，未交付：" + "、".join(unexpected))
    if missing:
        raise BugGitDeliveryError("本 Bug 的修改状态与快照不一致，未交付：" + "、".join(missing))

    title = str(bug_snapshot["title"]).strip()
    return BugRepairGitContext(
        bug_id=bug_id,
        workflow_id=str(workflow.get("id") or workflow.get("workflow_id") or ""),
        title=title,
        repo=repo,
        paths=tuple(paths),
        branch=branch,
        official_remote=remote,
        official_remote_url=official_remote_url,
        official_branch=remote_branch,
        base_commit=base_commit,
        subject=f"fix(ZENTAO):{bug_id} {title}",
    )


def _relay_branch(context: BugRepairGitContext, prefix: str) -> str:
    remote_path = context.official_remote_url.split(":", 1)[-1]
    remote_path = re.sub(r"\.git$", "", remote_path, flags=re.IGNORECASE)
    repository = re.sub(r"[^A-Za-z0-9._-]+", "-", remote_path).strip("-.") or "repository"
    workflow = re.sub(r"[^A-Za-z0-9]+", "", context.workflow_id)[-8:] or context.base_commit[:8]
    clean_prefix = re.sub(r"[^A-Za-z0-9._/-]+", "-", prefix).strip("/.") or "bug"
    return f"{clean_prefix}/{context.bug_id}/{repository}-{workflow}"


def _remote_ref(repo: Path, remote_url: str, branch: str) -> str | None:
    result = _run(repo, "ls-remote", "--heads", remote_url, f"refs/heads/{branch}", check=False)
    if result.returncode != 0:
        raise BugGitDeliveryError(f"无法读取 example_user 中转仓库：{_message(result) or '请检查网络或权限'}")
    line = result.stdout.decode("utf-8", "replace").strip()
    return line.split(None, 1)[0] if line else None


def _relay_report(
    context: BugRepairGitContext,
    *,
    relay_url: str,
    branch: str,
    commit: str,
) -> str:
    return "\n".join(
        [
            f"Bug #{context.bug_id} 的修改已发布到 example_user 中转仓库，尚未提交公司正式仓库。",
            f"公司代码库：{context.official_remote_url}",
            f"公司基准分支：{context.official_branch}",
            f"基准 Commit：{context.base_commit}",
            f"中转仓库：{relay_url}",
            f"中转分支：{branch}",
            f"修改 Commit：{commit}",
            f"提交备注：{context.subject}",
            "修改文件：" + "、".join(context.paths),
            "",
            f"发送“拉取 #{context.bug_id}”，系统会把 example_user 代码库中本次文件的当前修改写入你的个人开发代码库，保持未提交状态。",
            f"测试通过后发送“提交拉取并推送 #{context.bug_id}”，系统才会在同一个人本地代码库提交、拉取公司代码并推送。",
        ]
    )


def publish_bug_repair(workflow: dict[str, Any]) -> dict[str, Any]:
    """Create a test commit without moving HEAD/index and push it only to the relay."""
    _require_git_delivery_enabled()
    with REPAIR_FILE_LOCK:
        context = _repair_context(workflow, require_exclusive=False)
        config = get_app_config().bug_analysis.git_relay
        relay_url = str(config.remote_url or "").strip()
        if not config.enabled or not relay_url:
            raise BugGitDeliveryError("example_user 中转仓库未启用或未配置，修改仍只保留在本地")
        if relay_url.rstrip("/") == context.official_remote_url.rstrip("/"):
            raise BugGitDeliveryError("中转仓库与公司正式仓库相同，已拒绝发布")
        branch = _relay_branch(context, config.branch_prefix)

        descriptor, index_name = tempfile.mkstemp(prefix="deerflow-bug-index-")
        os.close(descriptor)
        Path(index_name).unlink(missing_ok=True)
        env = {**os.environ, "GIT_INDEX_FILE": index_name}
        try:
            _run(context.repo, "read-tree", context.base_commit, env=env)
            added = _run(context.repo, "add", "-A", "--", *context.paths, check=False, env=env)
            if added.returncode != 0:
                raise BugGitDeliveryError(f"无法准备中转提交：{_message(added)}")
            staged = _run(context.repo, "diff", "--cached", "--name-only", "-z", env=env).stdout.split(b"\0")
            staged_paths = sorted(item.decode("utf-8", "surrogateescape") for item in staged if item)
            if not staged_paths:
                raise BugGitDeliveryError("本 Bug 没有可发布的代码差异")
            if set(staged_paths) - set(context.paths):
                raise BugGitDeliveryError("中转提交包含不属于本 Bug 的文件，已停止")
            tree = _run(context.repo, "write-tree", env=env).stdout.decode().strip()
            commit_date = _git_optional(context.repo, "show", "-s", "--format=%cI", context.base_commit)
            commit_env = env if not commit_date else {**env, "GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date}
            created = _run(
                context.repo,
                "commit-tree",
                tree,
                "-p",
                context.base_commit,
                "-m",
                context.subject,
                check=False,
                env=commit_env,
            )
            if created.returncode != 0:
                raise BugGitDeliveryError(f"创建中转提交失败：{_message(created)}")
            commit = created.stdout.decode().strip()
        finally:
            Path(index_name).unlink(missing_ok=True)

        existing = _remote_ref(context.repo, relay_url, branch)
        if existing and existing != commit:
            raise BugGitDeliveryError(f"中转分支 {branch} 已存在其他提交，未覆盖")
        if existing != commit:
            pushed = _run(context.repo, "push", relay_url, f"{commit}:refs/heads/{branch}", check=False)
            if pushed.returncode != 0:
                return {
                    "status": "push_failed",
                    "repo_root": str(context.repo),
                    "relay_remote": config.remote_name,
                    "relay_url": relay_url,
                    "relay_branch": branch,
                    "test_commit": commit,
                    "base_commit": context.base_commit,
                    "company_remote": context.official_remote,
                    "company_remote_url": context.official_remote_url,
                    "company_branch": context.official_branch,
                    "files": staged_paths,
                    "subject": context.subject,
                    "report": f"本地修改已保留，但发布到 example_user 中转仓库失败：{_message(pushed) or '请检查网络或权限'}",
                }
        return {
            "status": "published",
            "repo_root": str(context.repo),
            "relay_remote": config.remote_name,
            "relay_url": relay_url,
            "relay_branch": branch,
            "test_commit": commit,
            "base_commit": context.base_commit,
            "company_remote": context.official_remote,
            "company_remote_url": context.official_remote_url,
            "company_branch": context.official_branch,
            "files": staged_paths,
            "subject": context.subject,
            "report": _relay_report(
                context,
                relay_url=relay_url,
                branch=branch,
                commit=commit,
            ),
        }


def _normalized_remote_url(value: str) -> str:
    return re.sub(r"\.git$", "", value.strip().rstrip("/"), flags=re.IGNORECASE)


def _sourcetree_repository_paths() -> list[Path]:
    browser = Path.home() / "Library/Application Support/SourceTree/browser.plist"
    if not browser.is_file():
        raise BugGitDeliveryError("没有读取到 SourceTree 本地仓库列表，无法定位你的代码库")
    try:
        archive = plistlib.loads(browser.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise BugGitDeliveryError("SourceTree 本地仓库列表不可读，无法定位你的代码库") from exc
    objects = archive.get("$objects") if isinstance(archive, dict) else None
    if not isinstance(objects, list):
        raise BugGitDeliveryError("SourceTree 本地仓库列表格式不受支持")

    def resolve(value: Any) -> Any:
        if isinstance(value, plistlib.UID) and value.data < len(objects):
            return objects[value.data]
        return value

    paths: list[Path] = []
    for item in objects:
        if not isinstance(item, dict) or resolve(item.get("isLeaf")) is not True:
            continue
        raw_path = resolve(item.get("path"))
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = Path(raw_path).expanduser().resolve()
        root = _git_optional(path, "rev-parse", "--show-toplevel") if path.is_dir() else None
        if root and Path(root).resolve() == path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _personal_repository(*, official_url: str) -> Path:
    expected = _normalized_remote_url(official_url)
    personal_root = (Path.home() / "projects").resolve()
    matches: list[Path] = []
    for candidate in _sourcetree_repository_paths():
        if candidate != personal_root and personal_root not in candidate.parents:
            continue
        remotes = (_git_optional(candidate, "remote") or "").splitlines()
        if any(_normalized_remote_url(_git_optional(candidate, "remote", "get-url", remote) or "") == expected for remote in remotes):
            matches.append(candidate)
    if not matches:
        raise BugGitDeliveryError("SourceTree 中没有找到对应的个人本地代码库")
    if len(matches) != 1:
        raise BugGitDeliveryError("SourceTree 中找到多个对应的个人本地代码库，无法确定写入位置：" + "、".join(str(path) for path in matches))
    return matches[0]


def _relay_patch(repo: Path, *, base_commit: str, relay_commit: str, paths: list[str]) -> bytes:
    if _git_optional(repo, "cat-file", "-t", base_commit) != "commit" or _git_optional(repo, "cat-file", "-t", relay_commit) != "commit":
        raise BugGitDeliveryError("本次中转提交或代码基准不存在")
    patch = _run(repo, "diff", "--binary", base_commit, relay_commit, "--", *paths).stdout
    if not patch:
        raise BugGitDeliveryError("本次中转提交没有代码差异")
    return patch


def _apply_patch(repo: Path, patch: bytes, *, reverse: bool = False, check: bool = False) -> subprocess.CompletedProcess[bytes]:
    args = ["git", "-C", str(repo), "apply", "--whitespace=nowarn"]
    if reverse:
        args.append("--reverse")
    if check:
        args.append("--check")
    args.append("-")
    return subprocess.run(args, input=patch, capture_output=True)


def fetch_bug_repair_for_test(workflow: dict[str, Any]) -> dict[str, Any]:
    """Write the relay repair into the personal checkout as uncommitted changes."""
    _require_git_delivery_enabled()
    with REPAIR_FILE_LOCK:
        previous = workflow.get("local_test_delivery")
        if isinstance(previous, dict) and previous.get("status") == "applied":
            return dict(previous)
        relay = workflow.get("relay_delivery")
        if not isinstance(relay, dict) or relay.get("status") != "published":
            raise BugGitDeliveryError("本次修改尚未发布到 example_user 中转仓库")

        bug_id = int(workflow.get("bug_id") or 0)
        relay_url = str(relay.get("relay_url") or "").strip()
        relay_branch = str(relay.get("relay_branch") or "").strip()
        relay_commit = str(relay.get("test_commit") or "").strip()
        base_commit = str(relay.get("base_commit") or "").strip()
        official_url = str(relay.get("company_remote_url") or "").strip()
        subject = str(relay.get("subject") or "").strip()
        paths = sorted(str(path) for path in relay.get("files") or [] if isinstance(path, str) and path)
        if bug_id <= 0 or not all((relay_url, relay_branch, relay_commit, base_commit, official_url, subject, paths)):
            raise BugGitDeliveryError("本次中转交付记录不完整，无法拉取到个人本地代码库")

        repo = _personal_repository(official_url=official_url)
        branch, remote, remote_branch = _tracking(repo)
        remote_url = _git_optional(repo, "remote", "get-url", remote) or ""
        if _normalized_remote_url(remote_url) != _normalized_remote_url(official_url):
            raise BugGitDeliveryError(f"个人本地仓库当前分支 {branch} 跟踪的不是本次公司代码库")

        fetched = _run(repo, "fetch", relay_url, relay_branch, check=False)
        if fetched.returncode != 0:
            raise BugGitDeliveryError(f"从中转仓库取得修改失败：{_message(fetched) or '请检查网络或权限'}")
        if _git_optional(repo, "rev-parse", "FETCH_HEAD") != relay_commit:
            raise BugGitDeliveryError("取得的中转提交与本次修复记录不一致")
        patch = _relay_patch(repo, base_commit=base_commit, relay_commit=relay_commit, paths=paths)
        checked = _apply_patch(repo, patch, check=True)
        if checked.returncode != 0:
            raise BugGitDeliveryError(f"本次修复与个人本地代码冲突，未写入任何修改：{_message(checked)}")
        applied = _apply_patch(repo, patch)
        if applied.returncode != 0:
            raise BugGitDeliveryError(f"本次修复未能写入个人本地代码库：{_message(applied)}")

        return {
            "status": "applied",
            "repository_path": str(repo),
            "branch": branch,
            "relay_commit": relay_commit,
            "base_commit": base_commit,
            "relay_branch": relay_branch,
            "company_remote": remote,
            "company_remote_url": remote_url,
            "company_branch": remote_branch,
            "files": paths,
            "subject": subject,
            "report": (f"Bug #{bug_id} 的修改已写入你的个人本地代码库。\n代码库：{repo}\n当前分支：{branch}\n修改保持未提交状态；没有创建或切换分支，也没有提交或推送。"),
        }


def sync_bug_relay(relay_delivery: dict[str, Any], *, restore: bool) -> dict[str, Any]:
    """Withdraw a rolled-back relay branch or republish it after restore."""
    _require_git_delivery_enabled()
    with REPAIR_FILE_LOCK:
        repo = Path(str(relay_delivery.get("repo_root") or "")).expanduser().resolve()
        if not repo.is_dir():
            # New records intentionally do not expose repo_root publicly, but
            # retain it server-side so rollback/restore can synchronize relay.
            raise BugGitDeliveryError("中转记录缺少本地仓库，无法同步回退状态")
        relay_url = str(relay_delivery.get("relay_url") or "")
        branch = str(relay_delivery.get("relay_branch") or "")
        commit = str(relay_delivery.get("test_commit") or "")
        if not relay_url or not branch or not commit:
            raise BugGitDeliveryError("中转记录不完整，无法同步回退状态")
        if restore:
            pushed = _run(repo, "push", relay_url, f"{commit}:refs/heads/{branch}", check=False)
            status = "published" if pushed.returncode == 0 else "sync_failed"
            action = "已重新发布" if pushed.returncode == 0 else "恢复成功，但重新发布失败"
        else:
            pushed = _run(repo, "push", f"--force-with-lease=refs/heads/{branch}:{commit}", relay_url, f":refs/heads/{branch}", check=False)
            status = "withdrawn" if pushed.returncode == 0 else "sync_failed"
            action = "已撤下" if pushed.returncode == 0 else "本地回退成功，但撤下中转分支失败"
        detail = "" if pushed.returncode == 0 else f"：{_message(pushed) or '请检查网络或权限'}"
        return {**relay_delivery, "status": status, "report": f"{action} example_user/{branch}{detail}"}


def _apply_stash(repo: Path, stash_oid: str) -> subprocess.CompletedProcess[bytes]:
    return _run(repo, "stash", "apply", "--index", stash_oid, check=False)


def _drop_delivery_stash(repo: Path, stash_oid: str) -> None:
    """Drop only the stash entry created by this delivery attempt."""
    if _git_optional(repo, "rev-parse", "stash@{0}") == stash_oid:
        _run(repo, "stash", "drop", "stash@{0}", check=False)


def _stash_worktree(repo: Path, *, bug_id: int) -> tuple[str | None, subprocess.CompletedProcess[bytes]]:
    before = _git_optional(repo, "rev-parse", "refs/stash")
    result = _run(repo, "stash", "push", "--include-untracked", "-m", f"deerflow-bug-{bug_id}-delivery", check=False)
    after = _git_optional(repo, "rev-parse", "refs/stash")
    return (after if result.returncode == 0 and after and after != before else None), result


def _restore_stash(repo: Path, stash_oid: str | None) -> str | None:
    if not stash_oid:
        return None
    restored = _apply_stash(repo, stash_oid)
    if restored.returncode != 0:
        return f"原有未提交修改仍保留在 Git stash {stash_oid[:12]} 中：{_message(restored)}"
    _drop_delivery_stash(repo, stash_oid)
    return None


def _commit_current_target_files(
    repo: Path,
    *,
    branch: str,
    paths: list[str],
    subject: str,
) -> tuple[str, str]:
    """Commit the current complete state of only the original repair files."""
    old_head = _git_optional(repo, "rev-parse", "HEAD") or ""
    if not old_head:
        raise BugGitDeliveryError("个人本地代码库当前提交不可读")

    descriptor, index_name = tempfile.mkstemp(prefix="deerflow-personal-index-")
    os.close(descriptor)
    Path(index_name).unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": index_name}
    try:
        _run(repo, "read-tree", old_head, env=env)
        added = _run(repo, "add", "-A", "--", *paths, check=False, env=env)
        if added.returncode != 0:
            raise BugGitDeliveryError(f"无法读取本次修复文件的当前修改：{_message(added)}")
        staged = _run(repo, "diff", "--cached", "--name-only", "-z", env=env).stdout.split(b"\0")
        staged_paths = sorted(item.decode("utf-8", "surrogateescape") for item in staged if item)
        if not staged_paths:
            raise BugGitDeliveryError("本次修复文件当前没有可提交的修改")
        if set(staged_paths) - set(paths):
            raise BugGitDeliveryError("准备提交的文件超出本次 Bug 的原始文件范围")
        tree = _run(repo, "write-tree", env=env).stdout.decode().strip()
        created = _run(repo, "commit-tree", tree, "-p", old_head, "-m", subject, check=False, env=_formal_submit_env(env))
        if created.returncode != 0:
            raise BugGitDeliveryError(f"创建 fix 提交失败：{_message(created)}")
        commit = created.stdout.decode().strip()
    finally:
        Path(index_name).unlink(missing_ok=True)

    moved = _run(repo, "update-ref", f"refs/heads/{branch}", commit, old_head, check=False)
    if moved.returncode != 0:
        raise BugGitDeliveryError(f"更新个人本地分支失败：{_message(moved)}")
    reset = _run(repo, "reset", "--mixed", "HEAD", "--", *paths, check=False)
    if reset.returncode != 0:
        _run(repo, "update-ref", f"refs/heads/{branch}", old_head, commit, check=False)
        raise BugGitDeliveryError(f"提交后无法恢复原有工作区状态：{_message(reset)}")
    return old_head, commit


def _retry_push(delivery: dict[str, Any]) -> dict[str, Any]:
    repo = Path(str(delivery.get("repo_root") or "")).expanduser().resolve()
    commit = str(delivery.get("commit") or "")
    branch = str(delivery.get("branch") or "")
    remote = str(delivery.get("remote") or "")
    remote_branch = str(delivery.get("remote_branch") or "")
    bug_id = int(delivery.get("bug_id") or 0)
    if not all((repo.is_dir(), commit, branch, remote, remote_branch, bug_id > 0)):
        raise BugGitDeliveryError("上次提交记录不完整，不能自动重试推送")
    if _git_optional(repo, "symbolic-ref", "--short", "HEAD") != branch or _git_optional(repo, "rev-parse", "HEAD") != commit:
        raise BugGitDeliveryError("提交后的仓库状态已经变化，不能自动重试推送")
    stash_oid, stashed = _stash_worktree(repo, bug_id=bug_id)
    if stashed.returncode != 0:
        raise BugGitDeliveryError(f"无法保留个人本地仓库原有修改：{_message(stashed)}")
    pull = _run(repo, "pull", "--rebase", remote, remote_branch, check=False, env=_formal_submit_env())
    if pull.returncode != 0:
        _run(repo, "rebase", "--abort", check=False)
        restore_error = _restore_stash(repo, stash_oid)
        suffix = f"\n{restore_error}" if restore_error else ""
        return {**delivery, "status": "conflict", "stash": stash_oid if restore_error else None, "report": f"fix 提交已保留；拉取 {remote}/{remote_branch} 时发生冲突，未推送。{suffix}"}
    commit = _git_optional(repo, "rev-parse", "HEAD") or commit
    push = _run(repo, "push", remote, f"HEAD:{remote_branch}", check=False)
    restore_error = _restore_stash(repo, stash_oid)
    suffix = f"\n{restore_error}" if restore_error else ""
    if push.returncode != 0:
        return {**delivery, "status": "push_failed", "commit": commit, "stash": stash_oid if restore_error else None, "report": f"代码已提交并拉取公司代码，但推送失败：{_message(push) or '请检查网络或权限'}{suffix}"}
    return {**delivery, "status": "submitted", "commit": commit, "stash": stash_oid if restore_error else None, "report": f"已提交、拉取并推送到 {remote}/{remote_branch}，提交 {commit[:12]}。{suffix}"}


def deliver_bug_repair(workflow: dict[str, Any]) -> dict[str, Any]:
    """Commit in the tested personal checkout, then pull and push its tracked branch."""
    _require_git_delivery_enabled()
    with REPAIR_FILE_LOCK:
        previous = workflow.get("git_delivery")
        if isinstance(previous, dict):
            if previous.get("status") == "submitted":
                return dict(previous)
            if previous.get("status") in {"push_failed", "conflict"} and previous.get("commit"):
                return _retry_push(previous)

        relay = workflow.get("relay_delivery")
        if not isinstance(relay, dict) or relay.get("status") != "published":
            raise BugGitDeliveryError("本次修改尚未发布到 example_user 中转仓库并完成测试交接，未提交公司正式仓库")
        local = workflow.get("local_test_delivery")
        if not isinstance(local, dict) or local.get("status") != "applied":
            raise BugGitDeliveryError("请先发送“拉取 #BugID”，把本次修改写入个人本地代码库并完成测试")
        bug_id = int(workflow.get("bug_id") or 0)
        repo = Path(str(local.get("repository_path") or "")).expanduser().resolve()
        branch = str(local.get("branch") or "")
        relay_commit = str(local.get("relay_commit") or "")
        base_commit = str(local.get("base_commit") or "")
        paths = sorted(str(path) for path in local.get("files") or [] if isinstance(path, str) and path)
        subject = str(local.get("subject") or "")
        remote = str(local.get("company_remote") or "")
        remote_url = str(local.get("company_remote_url") or "")
        remote_branch = str(local.get("company_branch") or "")
        if bug_id <= 0 or not (repo / ".git").exists() or not all((branch, relay_commit, base_commit, paths, subject, remote, remote_url, remote_branch)):
            raise BugGitDeliveryError("个人本地代码库交付记录不完整，未提交")
        if _git_optional(repo, "symbolic-ref", "--short", "HEAD") != branch:
            raise BugGitDeliveryError(f"个人本地代码库已离开测试时的分支 {branch}，未提交")
        current_remote_url = _git_optional(repo, "remote", "get-url", remote) or ""
        if _normalized_remote_url(current_remote_url) != _normalized_remote_url(remote_url):
            raise BugGitDeliveryError("个人本地代码库的公司远程已变化，未提交")
        if relay.get("test_commit") != relay_commit or relay.get("base_commit") != base_commit or sorted(relay.get("files") or []) != paths:
            raise BugGitDeliveryError("个人本地代码库中的测试修改与中转记录不一致，未提交")

        old_head, commit = _commit_current_target_files(repo, branch=branch, paths=paths, subject=subject)
        stash_oid, stashed = _stash_worktree(repo, bug_id=bug_id)
        if stashed.returncode != 0:
            _run(repo, "reset", "--mixed", old_head, check=False)
            raise BugGitDeliveryError(f"无法保留个人本地仓库原有修改，未拉取或推送：{_message(stashed)}")

        pull = _run(repo, "pull", "--rebase", remote, remote_branch, check=False, env=_formal_submit_env())
        if pull.returncode != 0:
            _run(repo, "rebase", "--abort", check=False)
            _run(repo, "reset", "--mixed", old_head, check=False)
            restore_error = _restore_stash(repo, stash_oid)
            suffix = f"\n{restore_error}" if restore_error else ""
            return {
                "status": "conflict",
                "bug_id": bug_id,
                "repo_root": str(repo),
                "branch": branch,
                "remote": remote,
                "remote_branch": remote_branch,
                "files": paths,
                "stash": stash_oid if restore_error else None,
                "report": f"已撤销 fix 提交；拉取 {remote}/{remote_branch} 时发生冲突，未推送。本次 Bug 修改已恢复为未提交状态。{suffix}",
            }

        commit = _git_optional(repo, "rev-parse", "HEAD") or commit
        result = {
            "status": "push_failed",
            "bug_id": bug_id,
            "repo_root": str(repo),
            "branch": branch,
            "remote": remote,
            "remote_branch": remote_branch,
            "files": paths,
            "stash": stash_oid,
            "commit": commit,
            "subject": subject,
        }
        pushed = _run(repo, "push", remote, f"HEAD:{remote_branch}", check=False)
        restore_error = _restore_stash(repo, stash_oid)
        suffix = f"\n{restore_error}" if restore_error else ""
        if pushed.returncode != 0:
            return {**result, "stash": stash_oid if restore_error else None, "report": f"代码已提交并拉取公司代码，但推送失败：{_message(pushed) or '请检查网络或权限'}{suffix}"}
        return {
            **result,
            "status": "submitted",
            "stash": stash_oid if restore_error else None,
            "report": f"Bug #{bug_id} 已提交、拉取并推送到 {remote}/{remote_branch}。\n提交：{commit[:12]}\n{subject}\n文件：" + "、".join(paths) + suffix,
        }
