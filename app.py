"""ZCode 存储迁移工具（tkinter 图形界面）。

把 C:\\Users\\DELL\\.zcode 这类数据根迁到其它磁盘，并建立目录联接，
让 ZCode 里硬编码的 ~/.zcode 路径自动落到新磁盘。
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import migrator as mg

APP_TITLE = "ZCode 存储迁移工具"
ZCODE_EXE = r"D:\Program Files\ZCode\ZCode.exe"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x700")
        self.minsize(860, 620)

        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel_flag = threading.Event()
        self.rollback_dir: Path | None = None

        self.source_var = tk.StringVar(value=str(mg.default_source()))
        self.target_var = tk.StringVar(value=str(mg.default_target()))
        self.set_env_var = tk.BooleanVar(value=True)

        self._build()
        self.after(120, self._pump)
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        paths = ttk.LabelFrame(root, text="路径", padding=10)
        paths.pack(fill="x")

        ttk.Label(paths, text="源目录（ZCode 当前数据根）").grid(row=0, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.source_var, width=76).grid(
            row=0, column=1, padx=6, pady=3, sticky="we"
        )
        ttk.Button(paths, text="浏览…", command=self._pick_source).grid(row=0, column=2, pady=3)

        ttk.Label(paths, text="目标目录（新磁盘）").grid(row=1, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.target_var, width=76).grid(
            row=1, column=1, padx=6, pady=3, sticky="we"
        )
        ttk.Button(paths, text="浏览…", command=self._pick_target).grid(row=1, column=2, pady=3)

        ttk.Checkbutton(
            paths,
            text="同时设置用户变量 ZCODE_STORAGE_DIR（只影响 cli\\exec、剪贴板等，不会移动数据库）",
            variable=self.set_env_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        paths.columnconfigure(1, weight=1)

        info = ttk.LabelFrame(root, text="当前状态", padding=10)
        info.pack(fill="x", pady=10)
        self.info_labels: dict[str, ttk.Label] = {}
        fields = [
            ("source", "源目录"),
            ("size", "数据体积"),
            ("drive", "磁盘剩余"),
            ("process", "ZCode 进程"),
            ("kind", "源目录类型"),
            ("env", "ZCODE_STORAGE_DIR"),
            ("base", "dataBaseDir"),
        ]
        for idx, (key, label) in enumerate(fields):
            ttk.Label(info, text=label, foreground="#555").grid(
                row=idx // 2, column=(idx % 2) * 2, sticky="w", padx=(0, 8), pady=2
            )
            value = ttk.Label(info, text="—")
            value.grid(row=idx // 2, column=(idx % 2) * 2 + 1, sticky="w", padx=(0, 28), pady=2)
            self.info_labels[key] = value

        actions = ttk.Frame(root)
        actions.pack(fill="x")
        self.buttons: list[ttk.Button] = []

        def add(text: str, command) -> None:
            btn = ttk.Button(actions, text=text, command=command, width=16)
            btn.pack(side="left", padx=(0, 6))
            self.buttons.append(btn)

        add("刷新状态", self.refresh)
        add("关闭 ZCode", self.on_stop)
        add("同步数据", self.on_sync)
        add("校验数据", self.on_verify)
        add("执行切换", self.on_switch)
        add("恢复原状", self.on_restore)
        add("清理回滚副本", self.on_cleanup)

        progress_box = ttk.Frame(root)
        progress_box.pack(fill="x", pady=(10, 4))
        self.progress = ttk.Progressbar(progress_box, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(progress_box, text="停止", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status_var).pack(fill="x")

        log_frame = ttk.LabelFrame(root, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(log_frame, height=14, wrap="none", state="disabled",
                           font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # --------------------------------------------------------------- helpers
    def source(self) -> Path:
        return Path(self.source_var.get().strip())

    def target(self) -> Path:
        return Path(self.target_var.get().strip())

    def _pick_source(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.source().parent))
        if chosen:
            self.source_var.set(chosen)

    def _pick_target(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.target().parent))
        if chosen:
            self.target_var.set(chosen)

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in self.buttons:
            btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self.cancel_flag.clear()

    def _cancel(self) -> None:
        self.cancel_flag.set()
        self.status_var.set("正在停止…")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.write(payload)
                elif kind == "progress":
                    copied, total, name = payload
                    if total:
                        self.progress.configure(value=copied * 100 / total)
                        self.status_var.set(
                            f"{copied / 1024**2:.0f} / {total / 1024**2:.0f} MB · {name}"
                        )
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self._set_busy(False)
                    callback = payload
                    if callback:
                        callback()
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def run_task(self, name: str, worker, on_done=None) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "已有任务在执行，请等待完成。")
            return
        self._set_busy(True)
        self.progress.configure(value=0)
        self.status_var.set(f"{name}…")
        self.write(f"\n=== {name} ===")

        def progress(copied: int, total: int, text: str) -> None:
            self.events.put(("progress", (copied, total, text)))

        def wrap() -> None:
            try:
                outcome = worker(progress)
            except Exception as exc:  # noqa: BLE001 - show any failure in the GUI
                self.events.put(("log", f"[失败] {type(exc).__name__}: {exc}"))
                self.events.put(("done", on_done))
                return
            self.events.put(("done", on_done))
            if isinstance(outcome, str) and outcome:
                self.events.put(("log", outcome))

        threading.Thread(target=wrap, daemon=True).start()

    # ---------------------------------------------------------------- status
    def refresh(self) -> None:
        src = self.source()
        st = mg.status(src)
        labels = self.info_labels
        labels["source"].configure(text=st["source"])
        labels["size"].configure(
            text=f"{st['files']} 个文件 · {st['bytes'] / 1024**3:.2f} GB" if st["exists"] else "不存在"
        )
        labels["drive"].configure(
            text=f"C {st['free_c'] / 1024**3:.2f} GB 可用 · F {st['free_f'] / 1024**3:.2f} GB 可用"
        )
        labels["process"].configure(
            text=f"{st['processes']} 个进程在运行" if st["processes"] else "未运行"
        )
        labels["kind"].configure(
            text="目录联接（已迁移）" if st["junction"] else "普通目录"
        )
        labels["env"].configure(text=st["storage_env"] or "（未设置）")
        labels["base"].configure(text=st["data_base_dir"] or "（未设置）")

        if st["junction"]:
            try:
                real = os.path.realpath(str(src))
            except OSError:
                real = "?"
            self.write(f"[状态] {src} 是目录联接，实际指向 {real}")
        if st["data_base_dir"]:
            self.write(
                f"[提示] setting.json 中 dataBaseDir={st['data_base_dir']}，"
                "桌面配置/任务索引仍写在该盘（不影响 cli\\db 与 cli\\log）。"
            )

    # --------------------------------------------------------------- actions
    def on_stop(self) -> None:
        def worker(progress):
            ok, msg = mg.stop_zcode()
            self.events.put(("log", msg))
            if not ok:
                self.events.put(("log", "请手动关闭 ZCode 后重试（关闭到托盘也要退出）。"))
            return None

        self.run_task("关闭 ZCode", worker, on_done=self.refresh)

    def on_sync(self) -> None:
        src, dst = self.source(), self.target()
        result = mg.preflight(src, dst)
        self.write(result.as_text())
        if not result.ok:
            messagebox.showerror(APP_TITLE, result.as_text())
            return

        running = bool(mg.zcode_processes())
        staging = False
        if running:
            choice = messagebox.askyesnocancel(
                APP_TITLE,
                "ZCode 仍在运行。\n\n"
                "是：先关闭 ZCode，再完整同步（推荐）\n"
                "否：只做预复制，暂不复制 SQLite/WAL（之后仍需最终同步）\n"
                "取消：不执行",
            )
            if choice is None:
                return
            if choice:
                ok, msg = mg.stop_zcode()
                self.write(msg)
                if not ok:
                    messagebox.showerror(APP_TITLE, "无法关闭 ZCode，请手动退出后重试。")
                    return
            else:
                staging = True

        drop = [] if staging else mg.drop_stale_sidecars(dst)
        for item in drop:
            self.write(f"[清理] 旧侧文件 {item}")

        stats = mg.sync_tree(
            src, dst,
            progress=lambda c, t, n: self.events.put(("progress", (c, t, n))),
            cancel=self.cancel_flag.is_set,
            exclude_sqlite=staging,
        )
        self.events.put(("log", f"[同步] 复制 {stats.copied_files} 个文件 "
                                f"{stats.copied_bytes / 1024**2:.1f} MB，跳过 {stats.skipped_files} 个"))
        for err in stats.errors[:20]:
            self.events.put(("log", f"[同步错误] {err}"))
        if stats.cancelled:
            self.events.put(("log", "[同步] 已取消"))
        elif not staging:
            cmp = mg.compare_trees(src, dst)
            self.events.put(("log", f"[校验] 源 {cmp['source_files']} 个 / {cmp['source_bytes'] / 1024**2:.1f} MB，"
                                    f"目标 {cmp['target_files']} 个 / {cmp['target_bytes'] / 1024**2:.1f} MB"))
            for missing in cmp["missing"]:
                self.events.put(("log", f"[缺失] {missing}"))
        return None

    def on_verify(self) -> None:
        def worker(progress):
            results = mg.verify_target(self.source(), self.target())
            if not results:
                self.events.put(("log", "[校验] 未找到数据库文件，跳过"))
                return None
            for item in results:
                flag = "OK" if item.ok else "FAIL"
                self.events.put(("log", f"[{flag}] {item.path} · tables={item.tables} · {item.detail}"))
            return None

        self.run_task("校验数据库", worker)

    def on_switch(self) -> None:
        src, dst = self.source(), self.target()
        result = mg.preflight(src, dst)
        self.write(result.as_text())
        if not result.ok:
            messagebox.showerror(APP_TITLE, result.as_text())
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "将执行：\n"
            "1. 关闭 ZCode\n"
            "2. 清理目标里的旧 WAL/SHM\n"
            "3. 增量同步全部数据\n"
            "4. 校验 SQLite 完整性（必须通过才继续）\n"
            f"5. 把 {src} 改名为 .rollback-时间戳 备份\n"
            f"6. 建立目录联接 {src} → {dst}\n"
            "7. 启动 ZCode\n\n"
            "原数据会保留为回滚副本，不会被删除。继续吗？",
        ):
            return

        def worker(progress):
            log = lambda text: self.events.put(("log", text))  # noqa: E731

            ok, msg = mg.stop_zcode()
            log(f"[关闭] {msg}")
            if not ok:
                return "已中止：ZCode 未完全退出。"

            for item in mg.drop_stale_sidecars(dst):
                log(f"[清理] 旧侧文件 {item}")

            stats = mg.sync_tree(src, dst,
                                 progress=lambda c, t, n: self.events.put(("progress", (c, t, n))),
                                 cancel=self.cancel_flag.is_set)
            log(f"[同步] 复制 {stats.copied_files} 个文件 {stats.copied_bytes / 1024**2:.1f} MB，"
                f"跳过 {stats.skipped_files} 个")
            if stats.cancelled or stats.errors:
                return "已中止：同步未完成或存在错误。"

            results = mg.verify_target(src, dst)
            failed = [r for r in results if not r.ok]
            for item in results:
                log(f"[{'OK' if item.ok else 'FAIL'}] {item.path} · {item.detail}")
            if failed:
                return "已中止：数据库校验未通过，未做任何切换。"

            ok, msg, rollback = mg.switch_to_target(src, dst, progress)
            log(f"[切换] {msg}")
            if not ok:
                return "切换失败，已尝试回滚。"

            self.rollback_dir = rollback
            if self.set_env_var.get():
                try:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f'[Environment]::SetEnvironmentVariable("ZCODE_STORAGE_DIR","{dst}","User")'],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    log(f"[环境] 已设置 ZCODE_STORAGE_DIR={dst}（下次启动生效）")
                except OSError as exc:
                    log(f"[环境] 设置失败: {exc}")

            ok, msg = mg.start_zcode(ZCODE_EXE)
            log(f"[启动] {msg}")
            return "切换完成。请确认 ZCode 能正常打开会话；确认无误后可清理回滚副本。"

        self.run_task("执行切换", worker, on_done=self.refresh)

    def on_restore(self) -> None:
        src = self.source()
        candidates = sorted(Path(src).parent.glob(f"{Path(src).name}.rollback-*"))
        if self.rollback_dir and self.rollback_dir.exists():
            candidates = [self.rollback_dir] + [c for c in candidates if c != self.rollback_dir]
        if not candidates:
            messagebox.showinfo(APP_TITLE, "没有找到回滚副本（.rollback-*）。")
            return
        chosen = candidates[0]
        if not messagebox.askyesno(
            APP_TITLE,
            f"将移除目录联接 {src}，并把回滚副本\n{chosen.name}\n改回原路径。\n\n继续吗？",
        ):
            return

        def worker(progress):
            ok, msg = mg.stop_zcode()
            self.events.put(("log", f"[关闭] {msg}"))
            if not ok:
                return "已中止：ZCode 未完全退出。"
            ok, msg = mg.restore_original(src, chosen)
            self.events.put(("log", f"[恢复] {msg}"))
            if ok:
                mg.start_zcode(ZCODE_EXE)
            return None

        self.run_task("恢复原状", worker, on_done=self.refresh)

    def on_cleanup(self) -> None:
        src = Path(self.source())
        candidates = sorted(src.parent.glob(f"{src.name}.rollback-*"))
        if not candidates:
            messagebox.showinfo(APP_TITLE, "没有可清理的回滚副本。")
            return
        chosen = candidates[0]
        files, size = mg.dir_stats(chosen)
        if not messagebox.askyesno(
            APP_TITLE,
            f"将永久删除：\n{chosen}\n{files} 个文件，约 {size / 1024**3:.2f} GB。\n\n"
            "删除后无法回滚到迁移前状态。确定吗？",
        ):
            return

        def worker(progress):
            ok, msg = mg.cleanup_rollback(chosen)
            self.events.put(("log", f"[清理] {msg}"))
            return None

        self.run_task("清理回滚副本", worker, on_done=self.refresh)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
