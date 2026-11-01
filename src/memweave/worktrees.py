import os
from pathlib import Path


def _pointer(value: str, base: Path) -> Path:
    path = Path(value.strip().strip('"'))
    if not path.is_absolute():
        path = base / path
    return path.expanduser().resolve()


def repository_root(path: Path) -> Path | None:
    """Return the nearest working-tree root containing a .git entry."""
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def git_common_dir(path: Path) -> Path | None:
    """Resolve the shared Git directory for a checkout or linked worktree."""
    root = repository_root(path)
    if root is None:
        return None
    marker = root / ".git"
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        return None
    try:
        pointer = marker.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    if not pointer.lower().startswith("gitdir:"):
        return None
    git_dir = _pointer(pointer.split(":", 1)[1], root)
    common_file = git_dir / "commondir"
    if not common_file.is_file():
        return git_dir
    try:
        common = common_file.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    return _pointer(common, git_dir) if common else git_dir


def primary_worktree_root(path: Path) -> Path:
    """Return the primary checkout root for a repository, else the input directory."""
    root = repository_root(path)
    if root is None:
        return Path(path).expanduser().resolve()
    common = git_common_dir(root)
    if common is not None and common.name == ".git" and common.parent.is_dir():
        return common.parent.resolve()
    return root


def same_repository(left: Path, right: Path) -> bool:
    left_common = git_common_dir(left)
    right_common = git_common_dir(right)
    return bool(
        left_common is not None
        and right_common is not None
        and os.path.normcase(str(left_common)) == os.path.normcase(str(right_common))
    )


def linked_worktrees(path: Path) -> list[Path]:
    """Return existing linked worktrees for the repository containing path."""
    common = git_common_dir(path)
    if common is None:
        return []
    metadata_root = common / "worktrees"
    if not metadata_root.is_dir():
        return []
    primary = primary_worktree_root(path)
    roots: list[Path] = []
    for metadata in metadata_root.iterdir():
        pointer_file = metadata / "gitdir"
        if not pointer_file.is_file():
            continue
        try:
            pointer = _pointer(pointer_file.read_text(encoding="utf-8-sig"), metadata)
        except (OSError, UnicodeError):
            continue
        if pointer.name != ".git":
            continue
        root = pointer.parent.resolve()
        if root == primary or not root.is_dir() or not (root / ".git").exists():
            continue
        if root not in roots:
            roots.append(root)
    return sorted(roots, key=lambda value: os.path.normcase(str(value)))
