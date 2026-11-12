---
title: 把 ZCode 数据根迁到 F 盘：为什么改环境变量没用，junction 才是解
description: 记录 C 盘被 ZCode 数据写满后触发 database or disk is full 的完整处理：三套互不相干的数据根、为什么 ZCODE_STORAGE_DIR 只能搬一部分、目录联接怎么做、以及迁移后如何验证写入真的落在新盘。
date: 2026-11-12
lastUpdated: 2026-11-12
tags:
  - ZCode
  - Windows
  - 存储迁移
  - junction
---

# 把 ZCode 数据根迁到 F 盘：为什么改环境变量没用，junction 才是解

## 结论先说

报错是 `database or disk is full`，但内存/CPU 都正常，原因是 C 盘只剩 1 GB 出头，而应用的数据根占着 10.7 GB 且**不在**设置里。

关键认知：**设置环境变量只能搬一部分**。真正吃空间的是硬编码在 `%USERPROFILE%\.zcode` 下的数据库、日志和设置。要一次性全部搬走，得用 NTFS **目录联接（junction）**：把真实目录移到新盘，再让原路径指向它。

## 现象

```text
Turn execution failed
database or disk is full
```

同一时段还有衍生错误：

```text
usage.tool.write.failed   database or disk is full
```

这类报错有个特征：**不影响读，只影响写**。会话还能翻，但新消息、用量记录、工具结果落不了盘。因为 SQLite 在 WAL 模式下追加事务时磁盘没有余量。

## 第一步：先确认到底是哪套目录

同一次排查里发现同一个应用至少有 **三套互不相干**的"根目录"：

| 开关 | 实际作用 |
| --- | --- |
| `ZCODE_STORAGE_DIR` | `cli/exec`、剪贴板、打包工具解包目录 |
| 桌面数据根（`dataBaseDir` / `*_DATA_BASE_DIR`） | 桌面配置、任务索引、主日志 |
| 代码里硬编码的 `~/.zcode` | **会话数据库、运行日志、设置文件** |

前两者走配置，第三个走代码。所以：

- 只设环境变量 → 数据库和日志照旧写 C 盘；
- 只改桌面数据根 → 同上；
- 反复重启、换 key、清缓存 → 完全没用。

验证办法很简单：看文件更新时间。

```text
cli\db\db.sqlite-wal        ← 数据库还在 C 盘增长
cli\log\zcode-<日期>.jsonl  ← 日志还在 C 盘增长
cli\exec\...                ← 这个才跟着环境变量走
```

一眼就能看出"哪些文件不听话"。

## 第二步：为什么不能只拷 SQLite

直接把 `db.sqlite` 拷到 F 盘、再改个配置指过去，是行不通的——代码里路径写死，没有可改的开关。

而且即使能改，也有坑：

- WAL 模式下，已提交事务可能还在 `-wal` 文件里，只拷主库会丢数据；
- 应用运行时拷贝，文件句柄和事务状态都不一致；
- `-shm` 是共享内存索引，跟主库版本必须匹配，不能随便留下旧文件。

安全做法只有两类：**停机拷贝**，或**用 SQLite 在线备份接口**（`sqlite3.Connection.backup`），后者可以在应用运行时拿到一致快照。

## 第三步：用 junction 让路径"就地搬家"

思路是把 `%USERPROFILE%\.zcode` 变成一个指向新盘的目录联接：

```text
1. 完全退出应用（含托盘进程、agent 子进程）
2. 把整个 ~/.zcode 复制到 F:\ZCodeData\.zcode
3. 校验：文件数、字节数、SQLite PRAGMA integrity_check
4. 把原目录改名成 .zcode.rollback-<时间戳>（保留可回滚）
5. mklink /J "C:\Users\<user>\.zcode" "F:\ZCodeData\.zcode"
6. 启动应用，确认新写入落在 F 盘
```

命令只有一条：

```bat
mklink /J "C:\Users\<user>\.zcode" "F:\ZCodeData\.zcode"
```

之后所有硬编码 `~/.zcode` 的代码都会透明地读写 F 盘，不需要改程序，也不需要管理员权限（`/J` 不需要）。

## 第四步：验证"真的搬过去了"

不要看界面提示，看文件：

| 检查项 | 通过标准 |
| --- | --- |
| 路径属性 | `C:\Users\<user>\.zcode` 是 `ReparsePoint`，Target 指向新盘 |
| 新盘目录 | 是普通目录（数据真的在那） |
| 写入落点 | 新日志/`db.sqlite-wal` 的 mtime 在增长，且 C/F 两个视角看到同一文件 |
| 回滚副本 | mtime 停在切换前，不再增长 |
| 数据库 | `PRAGMA integrity_check` 返回 `ok` |

本次实测：7,717 个文件 / 10.77 GB；切换后 `db.sqlite-wal` 与日志持续更新，回滚副本保持静态，两个库 `integrity_check` 都是 `ok`。

## 第五步：写工具时踩到的坑

手搓一遍之后就把它做成了图形化工具，过程中有几个必须处理的细节：

1. **不要在应用运行时改 SQLite 目录**。复制可以，切换不行；切换前必须确认进程全部退出（托盘的"关闭"往往只是隐藏窗口）。
2. **删联接不能递归**。`rmdir /s` 会把**目标盘的真实数据**一起删掉；只能用 `rmdir`（无 `/s`）或 `os.rmdir`。
3. **遍历要跳过重解析点**，否则目标里再有联接就会自循环。
4. **保留回滚副本**。迁完先留 `.zcode.rollback-*`，跑一两天没问题再删；删之前 C 盘的 10.7 GB 不会释放。
5. **预检查同盘**。目标与源在同一分区时迁移毫无意义，应该直接拒绝。

## 可复现工具

图形化（tkinter，零依赖）与命令行都在同一个仓库，包含预检、增量同步、SQLite 校验、改名回滚、建联接、恢复原状：

- GitHub：<https://github.com/Dongmingyue-loss/zcode-storage-migrator>
- 用法：`start.cmd` 打开界面，或 `python app.py`

界面里的按钮顺序就是上面的步骤；「执行切换」会把关闭应用 → 同步 → 校验 → 改名 → 建联接 → 启动串起来，任何一步失败都会中止而不是留下半切换状态。

## 经验总结

- **报错位置不等于问题位置。** `database or disk is full` 是结果，原因往往是某个目录悄悄长到把盘吃满。
- **先搞清楚"哪个开关管哪部分"。** 一个应用同时存在环境变量、桌面配置、硬编码路径三套根目录，很常见。
- **改代码路径不如改文件系统语义。** junction 让硬编码路径"就地生效"，比逐个打补丁可靠。
- **验证要看写入。** 路径改了不代表写过去了，必须看 mtime 和文件大小在不在变化。
- **回滚副本先留着。** 空间紧张时最容易冲动删备份，出了问题就没退路。

## 边界

- 仅 Windows（`mklink /J`）。
- 只迁移用户目录下的数据根；桌面配置根（`dataBaseDir`）是另一个独立开关，需要单独处理。
- 联接对备份/杀软工具可能表现为"重复计数"，属正常现象。
