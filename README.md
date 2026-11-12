# ZCode 存储迁移工具

把 ZCode 的数据根从几乎写满的 C 盘搬到其它磁盘，并让 ZCode 继续按原来的路径工作。
图形界面（tkinter），**零第三方依赖**，不做任何后台常驻。

## 为什么需要它

ZCode 3.x 里 `ZCODE_STORAGE_DIR` **只管一部分**：

| 位置 | 是否受 `ZCODE_STORAGE_DIR` 控制 |
|---|---|
| `cli\exec`、剪贴板、SEA 运行时工具 | 是 |
| `cli\db\db.sqlite`（会话数据库） | **否**，硬编码 `~/.zcode` |
| `cli\log\zcode-*.jsonl`（运行日志） | **否**，硬编码 `~/.zcode` |
| `v2\setting.json`（设置） | **否**，硬编码 `~/.zcode` |
| 桌面数据根 | 由 `setting.json` 的 `dataBaseDir` 决定，优先于环境变量 |

所以只设环境变量、只重启，C 盘那套仍会继续增长。正确做法是**目录联接（junction）**：
把真实目录搬到新盘，再让 `C:\Users\<你>\.zcode` 指向它。硬编码路径会透明地落到新盘。

## 用法

1. 双击 `start.cmd`（或 `python app.py`）。
2. 确认「源目录」是 `C:\Users\<你>\.zcode`，「目标目录」默认 `F:\ZCodeData\.zcode`。
3. 建议按顺序点：
   - **关闭 ZCode** —— 完全退出（含托盘/agent 进程）
   - **同步数据** —— 增量复制，已一致的文件自动跳过；结束时比对文件数与字节数
   - **校验数据** —— 对 `db.sqlite`、`tasks-index.sqlite` 执行 `PRAGMA integrity_check`
   - **执行切换** —— 关闭 → 清理旧 WAL/SHM → 同步 → 校验 → 改名备份 → 建联接 → 启动
4. ZCode 正常打开、历史会话可见后，再决定是否 **清理回滚副本**（永久删除，释放 C 盘空间）。

也可以直接点「执行切换」，它会在一次流程里做完上述所有步骤，并在任何一步失败时中止（不会留下半切换状态）。

## 安全设计

- **预检**：同盘、目标在源内、空间不足、源已是联接、目标已是联接，全部拒绝执行。
- **只改名不删除**：切换时把原目录改名为 `.rollback-<时间戳>`，不删除任何数据。
- **校验门禁**：SQLite `integrity_check` 不通过就不切换。
- **联接删除安全**：移除联接用 `os.rmdir`，绝不递归删除目标内容；`rmtree` 只用于回滚副本且先确认真实目录。
- **复制跳过联接**：遍历时跳过重解析点，避免自循环或越界复制。
- **停止可恢复**：同步过程可点「停止」，已复制的文件保留，下次继续增量。

## 回滚

界面点 **恢复原状**：关闭 ZCode → 移除联接 → 把最新 `.rollback-*` 改回原路径 → 启动。
注意：回滚会丢弃迁移之后写在新盘的数据增量。

手动等价操作（关闭 ZCode 后）：

```bat
rmdir "C:\Users\DELL\.zcode"                       REM 不要加 /s，只删联接
ren "C:\Users\DELL\.zcode.rollback-20261112-2130" ".zcode"
```

## 环境变量说明

界面里的「同时设置用户变量 ZCODE_STORAGE_DIR」默认勾选，它只影响 `cli\exec`、剪贴板等，
让这些零散文件也落到新盘；**它不负责迁移数据库**，迁移靠目录联接。

单独设置它（不建联接）不会解决 C 盘占用问题。

## 测试

```bat
cd /d F:\Grok-4.5\tools\zcode-storage-migrator
python -m unittest discover -s tests -v
```

覆盖：遍历统计、增量同步与跳过、取消、WAL 清理、SQLite 校验（正常/损坏/缺失）、
预检（同盘/嵌套/缺源）、回滚目录命名、联接创建与移除、拒绝删除普通目录。

## 限制

- 仅 Windows（依赖 `mklink /J` 与 WMI 进程查询）。
- 只迁移 `~/.zcode`；桌面数据根由 `setting.json` 的 `dataBaseDir` 单独决定（本工具只展示，不修改），
  如需一并搬走请在应用设置里改数据目录。
- 备份/杀软工具可能把联接目录重复计数，属正常现象。
