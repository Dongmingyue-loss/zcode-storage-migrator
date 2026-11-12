"""Core logic for relocating the ZCode data root to another volume.

Why this exists
---------------
ZCode 3.x computes most of its storage as ``~/.zcode`` in code, so
``ZCODE_STORAGE_DIR`` only redirects ``cli/exec``, clipboard and SEA
runtime tools.  Moving the whole tree therefore needs a directory
junction: rename the real folder, then point the old path at the new
drive.  These helpers do that with preflight checks so nothing is
deleted by accident.

Everything here is plain stdlib and safe to unit test with temp dirs.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

IS_WIN = os.name == "nt"
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")

Progress = Optional[Callable[[int, int, str], None]]
Cancel = Optional[Callable[[], bool]]


# --------------------------------------------------------------------------
# paths and detection
# --------------------------------------------------------------------------

def default_source() -> Path:
    return Path(os.path.expanduser("~")) / ".zcode"


def default_target() -> Path:
    anchor = "F:\\" if IS_WIN else "/"
    return Path(anchor) / "ZCodeData" / ".zcode"


def is_junction(path: Path) -> bool:
    """True for an NTFS junction (and, on POSIX, for a symlink)."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if not IS_WIN:
        return path.is_symlink()
    return getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def _is_reparse_dir(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) or getattr(
        st, "st_reparse_tag", 0
    ) == IO_REPARSE_TAG_MOUNT_POINT


def walk_files(root: Path, skip_reparse: bool = True) -> Iterable[tuple[Path, int, int]]:
    """Yield ``(path, size, mtime_ns)`` for regular files under ``root``.

    Reparse points are skipped so a junction inside the tree can never make
    the walk loop or escape the source directory.
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if skip_reparse and _is_reparse_dir(entry):
                                continue
                            stack.append(entry.path)
                            continue
                        if entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            yield Path(entry.path), st.st_size, st.st_mtime_ns
                    except OSError:
                        continue
        except OSError:
            continue


def dir_stats(root: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for ``root``."""
    files = 0
    total = 0
    for _path, size, _mtime in walk_files(root):
        files += 1
        total += size
    return files, total


def disk_free(path: Path) -> int:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(str(target)).free
    except OSError:
        return 0


def drive_of(path: Path) -> str:
    return Path(os.path.abspath(str(path))).drive.upper() if IS_WIN else "/"


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

@dataclass
class Preflight:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_text(self) -> str:
        lines = [f"错误: {e}" for e in self.errors]
        lines += [f"警告: {w}" for w in self.warnings]
        return "\n".join(lines) or "预检通过"


def preflight(source: Path, target: Path) -> Preflight:
    result = Preflight()
    source = Path(source)
    target = Path(target)

    if not source.exists():
        result.errors.append(f"源目录不存在: {source}")
        return result
    if is_junction(source):
        result.errors.append(
            f"源目录已经是目录联接/符号链接（{source}）。"
            "如需重新迁移，请先执行“恢复原状”。"
        )
    if (source / "cli" / "db" / "db.sqlite").exists() is False and not any(source.iterdir()):
        result.warnings.append("源目录看起来是空的，请确认路径是否正确。")
    if drive_of(source) == drive_of(target):
        result.errors.append(
            f"目标与源在同一磁盘（{drive_of(source)}），迁移不会释放空间。"
        )
    try:
        target.resolve().relative_to(source.resolve())
        result.errors.append("目标目录位于源目录内部，会造成递归复制。")
    except ValueError:
        pass
    try:
        source.resolve().relative_to(target.resolve())
        result.errors.append("源目录位于目标目录内部，会造成递归复制。")
    except ValueError:
        pass
    if is_junction(target):
        result.errors.append(
            f"目标目录已经是目录联接（{target}），请换一个空目录。"
        )
    if target.exists():
        files, size = dir_stats(target)
        if files:
            result.warnings.append(
                f"目标目录已有 {files} 个文件（{size / 1024**3:.2f} GB），同步会覆盖同名文件并保留多余文件。"
            )
    files, size = dir_stats(source)
    free = disk_free(target)
    if free and size * 1.05 > free:
        result.errors.append(
            f"目标磁盘空间不足：需要约 {size / 1024**3:.2f} GB，"
            f"可用 {free / 1024**3:.2f} GB。"
        )
    if sys.platform != "win32":
        result.errors.append("当前只支持 Windows（需要 mklink /J 目录联接）。")
    return result


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

@dataclass
class SyncStats:
    total_bytes: int = 0
    copied_bytes: int = 0
    copied_files: int = 0
    skipped_files: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def sync_tree(
    source: Path,
    target: Path,
    progress: Progress = None,
    cancel: Cancel = None,
    exclude_sqlite: bool = False,
) -> SyncStats:
    """Copy ``source`` into ``target``, skipping unchanged files.

    ``exclude_sqlite`` leaves live SQLite files alone; use it when ZCode is
    still running and you only want a staging copy.
    """
    stats = SyncStats()
    source = Path(source)
    target = Path(target)

    entries = list(walk_files(source))
    stats.total_bytes = sum(size for _p, size, _m in entries)

    copied = 0
    for path, size, mtime_ns in entries:
        if cancel and cancel():
            stats.cancelled = True
            break
        rel = path.relative_to(source)
        if exclude_sqlite and path.name.startswith("db.sqlite"):
            stats.skipped_files += 1
            continue
        dest = target / rel
        try:
            if dest.exists():
                dst = dest.stat()
                if dst.st_size == size and dst.st_mtime_ns == mtime_ns:
                    stats.skipped_files += 1
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied += size
            stats.copied_files += 1
            stats.copied_bytes += size
        except OSError as exc:
            stats.errors.append(f"{rel}: {exc}")
        if progress and (stats.copied_files % 20 == 0 or stats.copied_bytes == copied):
            progress(stats.copied_bytes, stats.total_bytes, str(rel))
    if progress:
        progress(stats.copied_bytes, stats.total_bytes, "done")
    return stats


def drop_stale_sidecars(target: Path) -> list[str]:
    """Remove ``-wal/-shm`` next to databases in ``target``.

    Call only when ZCode is stopped and the main database files are about to
    be copied; a stale WAL replayed against a fresh database would corrupt it.
    """
    removed = []
    for db in list(Path(target).rglob("*.sqlite")) + list(Path(target).rglob("*.db")):
        for suffix in SQLITE_SIDECARS:
            sidecar = db.with_name(db.name + suffix)
            if sidecar.exists() and not is_junction(sidecar):
                try:
                    sidecar.unlink()
                    removed.append(str(sidecar))
                except OSError:
                    pass
    return removed


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

@dataclass
class VerifyResult:
    path: str
    ok: bool
    detail: str
    tables: int = 0


def verify_sqlite(path: Path) -> VerifyResult:
    path = Path(path)
    if not path.exists():
        return VerifyResult(str(path), False, "文件不存在")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        try:
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = con.execute(
                "select count(*) from sqlite_master where type='table'"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return VerifyResult(str(path), False, f"SQLite 错误: {exc}")
    return VerifyResult(str(path), result == "ok", str(result), tables)


def verify_target(source: Path, target: Path) -> list[VerifyResult]:
    results = []
    for rel in ("cli/db/db.sqlite", "v2/tasks-index.sqlite"):
        src_db = Path(source) / rel
        if src_db.exists():
            results.append(verify_sqlite(Path(target) / rel))
    return results


def compare_trees(source: Path, target: Path) -> dict:
    src_files, src_bytes = dir_stats(source)
    dst_files, dst_bytes = dir_stats(target)
    missing = []
    for path, size, _mtime in walk_files(source):
        dest = Path(target) / path.relative_to(source)
        if not dest.exists() or dest.stat().st_size != size:
            missing.append(str(path.relative_to(source)))
            if len(missing) >= 20:
                break
    return {
        "source_files": src_files,
        "source_bytes": src_bytes,
        "target_files": dst_files,
        "target_bytes": dst_bytes,
        "missing": missing,
    }


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    """Console output on zh-CN Windows is usually GBK, but may be UTF-8."""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _powershell(script: str, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _decode(proc.stdout).strip()


def zcode_processes() -> list[dict]:
    if not IS_WIN:
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ZCode.exe' } | "
        "Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress"
    )
    try:
        raw = _powershell(script)
    except (OSError, subprocess.SubprocessError):
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def stop_zcode(force_after: float = 8.0) -> tuple[bool, str]:
    """Ask ZCode to close, then force whatever survives ``force_after``."""
    if not IS_WIN:
        return False, "仅支持 Windows"
    if not zcode_processes():
        return True, "ZCode 未运行"
    subprocess.run(["taskkill", "/IM", "ZCode.exe"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + force_after
    while time.time() < deadline:
        if not zcode_processes():
            return True, "ZCode 已退出"
        time.sleep(0.5)
    for proc in zcode_processes():
        pid = proc.get("ProcessId")
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(2)
    left = zcode_processes()
    if left:
        return False, f"仍有 {len(left)} 个 ZCode 进程未退出"
    return True, "ZCode 已强制退出"


def start_zcode(exe: str = r"D:\Program Files\ZCode\ZCode.exe") -> tuple[bool, str]:
    if not Path(exe).exists():
        return False, f"找不到 {exe}"
    try:
        subprocess.Popen([exe], close_fds=True)
    except OSError as exc:
        return False, str(exc)
    return True, "已启动 ZCode"


# --------------------------------------------------------------------------
# switch / rollback
# --------------------------------------------------------------------------

def rollback_path_for(source: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = source.parent / f"{source.name}.rollback-{stamp}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = source.parent / f"{base.name}-{index}"
        index += 1
    return candidate


def make_junction(link: Path, target: Path) -> tuple[bool, str]:
    link = Path(link)
    target = Path(target)
    if is_junction(link):
        return True, "目录联接已存在"
    if link.exists():
        return False, f"目标路径已存在且不是联接: {link}"
    cmd = ["cmd", "/c", "mklink", "/J", str(link), str(target)]
    proc = subprocess.run(cmd, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        return False, (_decode(proc.stdout) + _decode(proc.stderr)).strip() or "mklink 失败"
    return is_junction(link), "目录联接已创建"


def remove_junction(link: Path) -> tuple[bool, str]:
    """Remove a junction only. Never recurses into its target."""
    link = Path(link)
    if not link.exists():
        return True, "联接不存在"
    if not is_junction(link):
        return False, "该路径不是目录联接，拒绝删除（请手动确认）"
    try:
        os.rmdir(str(link))
    except OSError as exc:
        return False, f"删除联接失败: {exc}"
    return not link.exists(), "联接已移除，数据仍在目标目录"


def switch_to_target(
    source: Path,
    target: Path,
    progress: Progress = None,
) -> tuple[bool, str, Optional[Path]]:
    """Rename the live folder aside and point it at ``target``."""
    source = Path(source)
    target = Path(target)
    if zcode_processes():
        return False, "ZCode 仍在运行，请先关闭", None
    rollback = rollback_path_for(source)
    try:
        os.rename(str(source), str(rollback))
    except OSError as exc:
        return False, f"重命名原目录失败: {exc}", None
    ok, detail = make_junction(source, target)
    if not ok:
        try:
            os.rename(str(rollback), str(source))
        except OSError:
            pass
        return False, f"创建联接失败，已回退: {detail}", None
    if progress:
        progress(1, 1, "switch")
    return True, f"已切换到 {target}，原目录保留在 {rollback}", rollback


def restore_original(source: Path, rollback: Path) -> tuple[bool, str]:
    source = Path(source)
    rollback = Path(rollback)
    if not rollback.exists():
        return False, f"回滚目录不存在: {rollback}"
    if source.exists():
        if not is_junction(source):
            return False, "原路径已被真实目录占用，拒绝覆盖"
        ok, detail = remove_junction(source)
        if not ok:
            return False, detail
    try:
        os.rename(str(rollback), str(source))
    except OSError as exc:
        return False, f"恢复失败: {exc}"
    return True, "已恢复原目录"


def cleanup_rollback(rollback: Path) -> tuple[bool, str]:
    rollback = Path(rollback)
    if not rollback.exists():
        return True, "回滚目录不存在"
    if is_junction(rollback):
        return False, "拒绝删除联接，请先确认路径"
    files, size = dir_stats(rollback)
    try:
        shutil.rmtree(str(rollback))
    except OSError as exc:
        return False, f"删除失败: {exc}"
    return True, f"已删除 {files} 个文件，释放约 {size / 1024**3:.2f} GB"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def read_setting_data_base_dir(source: Path) -> Optional[str]:
    setting = Path(source) / "v2" / "setting.json"
    if not setting.exists():
        return None
    try:
        data = json.loads(setting.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("dataBaseDir")
    return value if isinstance(value, str) else None


def environment_value(name: str) -> str:
    if not IS_WIN:
        return os.environ.get(name, "")
    try:
        return _powershell(f'[Environment]::GetEnvironmentVariable("{name}","User")', timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""


def status(source: Path) -> dict:
    source = Path(source)
    files, size = dir_stats(source) if source.exists() else (0, 0)
    procs = zcode_processes()
    return {
        "source": str(source),
        "exists": source.exists(),
        "junction": is_junction(source),
        "files": files,
        "bytes": size,
        "processes": len(procs),
        "free_c": disk_free(Path("C:\\")),
        "free_f": disk_free(Path("F:\\")),
        "storage_env": environment_value("ZCODE_STORAGE_DIR"),
        "data_base_dir": read_setting_data_base_dir(source),
    }
