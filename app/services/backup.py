import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

_NAME_RE = re.compile(r"^remember-\d{8}-\d{6}(?:-\d{6})?(?:-\d+)?\.db$")


def backup_dir() -> Path:
    """Return the backups directory next to the SQLite database."""
    return Path(settings.database_path).parent / "backups"


async def create_backup() -> Path:
    """Copy the SQLite database into the backups directory; return the new path.

    The filename is unique even for rapid successive calls (Windows clock
    precision can repeat microseconds): a numeric suffix is added on collision.
    """
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    base = f"remember-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
    target = folder / f"{base}.db"
    suffix = 1
    while target.exists():
        target = folder / f"{base}-{suffix}.db"
        suffix += 1
    source = Path(settings.database_path)

    def _copy() -> None:
        src = sqlite3.connect(source)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    await asyncio.to_thread(_copy)
    return target


def list_backups() -> list[dict]:
    """Return backup metadata sorted by name descending (newest first)."""
    folder = backup_dir()
    if not folder.exists():
        return []
    items: list[dict] = []
    for path in folder.glob("remember-*.db"):
        if _NAME_RE.fullmatch(path.name) is None:
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC),
            }
        )
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


async def prune_backups(keep: int) -> int:
    """Delete the oldest backups leaving ``keep``; return how many were removed."""
    keep = max(1, min(keep, 100))
    backups = list_backups()
    removed = 0
    for item in sorted(backups, key=lambda entry: entry["name"]):
        remaining = len(backups) - (removed + 1)
        if remaining < keep:
            break
        (backup_dir() / item["name"]).unlink(missing_ok=True)
        removed += 1
    return removed


def resolve_backup(name: str) -> Path | None:
    """Resolve a backup name to an existing file inside the backups directory."""
    if _NAME_RE.fullmatch(name) is None:
        return None
    candidate = (backup_dir() / name).resolve()
    root = backup_dir().resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate