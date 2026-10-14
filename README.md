# 🛡️ Claude 1:1 本地数据复原与防封重放系统 (`claude-recovery-suite`)

针对研发与工作人员在使用 **Claude Desktop**（含 **Code 文件夹项目模式**、**Cowork 本地 Agent 空间模式**、**云端 Projects 与 Chat 模式**）及 **Claude Code CLI** 时，因账号突发封禁（如 `$200 Max` 套餐被封并触发客户端强制登出）导致所有项目文件夹结构、多轮对话记录（输入、AI 思考链、工具调用、AI 输出）及代码成果无法访问的问题，本工具提供 **100% 离线、零外部依赖、1:1 完整复原、可视化重放与新账号无损迁移** 解决方案。

---

## 💡 一、为什么账号被封后客户端所有数据“消失”？本机数据加密了吗？

经过对 Claude Desktop 与 Claude Code 本地存储架构的逆向取证分析，**本机所有核心数据均未加密**（分别采用明文 JSONL、LevelDB Snappy 压缩或 Chromium Simple Cache Zstandard 压缩 + CRC32 校验）。

当账号被封禁时，之所以在客户端看不到任何记录，原因有二：
1. **强制登出锁死界面**：Claude Desktop 检测到账号封禁时，会在 `Local Storage` 写入强制登出标记（`auth:last-signout: {"kind": "forced"}`）并清空未发送草稿，将界面锁死在登录页。
2. **账号/组织 UUID 命名空间隔离**：即使你在客户端登录了一个**新账号**，Claude Desktop 也会按 `<accountUuid>/<orgUuid>` 目录隔离本地数据，导致原被封账号下的所有项目、会话与空间在新账号下完全隐形。

### 本工具自动扫描并 1:1 复原的 5 层本地存储：

| 存储层级 | 本地物理路径（以 macOS 为例，同时支持 Windows / Linux） | 存储格式 / 压缩算法 | 复原内容 |
| :--- | :--- | :--- | :--- |
| **1. Code 项目文件夹与多轮对话** | `~/.claude/projects/`<br/>`~/Library/Application Support/Claude/claude-code-sessions/`<br/>`~/.claude/transcripts/` | 明文 `.jsonl` + `.json` | 所有项目文件夹映射、主会话与 Subagent 子会话完整多轮对话、AI `Thinking` 思考链、`Write`/`Edit`/`Bash` 工具调用与结果、项目专属 `MEMORY.md` 记忆、关联 GitHub PR 与 Artifact 看板 |
| **2. Cowork 文件夹空间与本地 Agent** | `~/Library/Application Support/Claude/local-agent-mode-sessions/` | `spaces.json` + `audit.jsonl` + `outputs/` | GUI 创建的全部文件夹项目空间（`spaces.json` 及自定义指令）、`.project-cache` 同步文档、本地 Agent 会话日志、全部 `outputs/` 产出文件与 `uploads/` 上传附件 |
| **3. 云端对话、云端项目与沙盒文件** | `~/Library/Application Support/Claude/Cache/Cache_Data/*_0`<br/>及 Chrome / Arc / Edge / Brave 浏览器缓存 | Chromium Simple Cache + **Zstandard (`zstd`)** + CRC32 | 从 HTTP 缓存块中经 CRC32 校验并解压还原纯云端多轮对话（`chat_conversations` 含全部历史响应版本）、云端 `Projects` 配置以及云端沙盒下载文件（`wiggle/download-file` 生成的 PDF/Word/Markdown/图片） |
| **4. GUI 侧边栏布局与置顶分组** | `~/Library/Application Support/Claude/Local Storage/leveldb/*.ldb` | LevelDB SSTable (**Snappy 压缩**) | 内置纯 Python Snappy 解压器，100% 还原客户端侧边栏的项目文件夹排序（`dframe-store`）与手动置顶分组（`dframe-group-scopes`） |
| **5. 历史文件快照、影子仓库与缺失目录逆向重构** | `~/.claude/file-history/`<br/>`~/Library/Application Support/Claude/git-shadow/`<br/>`~/.claude/handoff-history/` | 快照映射 + 影子 Git 库 + 工具流重放 | 自动通过 `trackedFileBackups` 将哈希快照还原为真实代码文件名；提取 `git-shadow` 影子 Git 仓库提交记录；**若本地项目文件夹已被误删，自动从会话 `outputs/` 与 `Write`/`Edit` 工具调用中 1:1 逆向重构出源码与成果文件** |

---

## 🚀 二、快速开始（零依赖，开箱即用）

本工具仅使用 Python 标准库（兼容 Python 3.9 ~ 3.14，无需 `pip install` 任何第三方包）。

### 方式 1：一键启动脚本（推荐）

克隆本仓库后，直接运行：

```bash
chmod +x start.sh
./start.sh
```

`start.sh` 会自动完成以下三步：
1. **防清理加固**：自动将 `~/.claude/settings.json` 中的 `"cleanupPeriodDays"` 设为 `3650`（10 年保留期，防止 Claude Code 默认 30 天自动删除历史 `.jsonl` 记录）；
2. **全盘 1:1 恢复导出**：首次运行时自动扫描本机 5 层存储，将恢复出的所有项目文件夹、多轮对话 Markdown 记录、重构代码与沙盒文件导出至 `./recovered_data/`；
3. **启动可视化工作台**：启动本地 Web 服务并自动在浏览器打开 **http://127.0.0.1:8765**。

---

### 方式 2：分步命令行使用

#### 1. 执行全量取证恢复与物理目录导出
```bash
python3 recover_claude.py
```
执行完成后，所有数据将按原项目结构 1:1 导出至 `./recovered_data/`：
- `recovered_data/projects/<项目名>/`
  - `project_info.json`：项目原始路径、Git Worktree 分支、关联 PR 与 Artifact 列表
  - `memory/`：该项目下恢复的 `MEMORY.md` 记忆文档
  - `conversations/*.md`：该项目下所有多轮对话的 1:1 Markdown 完整记录（含思考过程、工具调用与代码 Diff）
  - `reconstructed_code/`：从对话 `Write`/`Edit` 工具调用中提取还原的代码文件
- `recovered_data/cowork_spaces/`：所有 Cowork 本地 Agent 会话的 Markdown 记录
- `recovered_data/reconstructed_missing_folders/`：针对磁盘上已删除的本地项目文件夹，自动从对话记录与 `outputs/` 中逆向重构找回的目录与源码
- `recovered_data/cloud_chats_and_projects/`：从 Chromium Zstd 缓存还原的云端对话（`.md` / `.json` / 历史版本）与 `sandbox_files/` 云端沙盒文件

#### 2. 启动 1:1 本地 Web 复原工作台
```bash
python3 server.py --port 8765
```
在浏览器访问 **http://127.0.0.1:8765**，支持六大视图切换：
- **💻 Code 项目文件夹**：按项目文件夹展开查看所有历史多轮对话、折叠查看 AI `Thinking` 思考链、查看 `Write`/`Edit` 红绿语法 Diff、切换查看关联的 **Subagent 子会话**、直接浏览/预览本地项目源码文件、一键导出 `.md`。
- **🤝 Cowork 空间与会话**：查看全部 GUI 文件夹空间（`spaces.json`）与本地 Agent 会话，在线预览或下载所有 `outputs/` 产出文件与 `uploads/` 附件。
- **☁️ 云端对话与沙盒缓存**：查看从浏览器/客户端缓存解压还原的云端对话与沙盒生成文件（支持在线预览 PDF、图片、代码与 Markdown）。
- **📌 置顶与工作树**：1:1 还原客户端侧边栏的置顶分组与 Git Worktrees。
- **🕰️ 历史快照与影子库**：浏览已还原真实文件名的 `file-history` 历史代码快照、`git-shadow` 影子 Git 仓库提交日志、`handoff-history` 交接报告、`pending-uploads` 待传图片与 `paste-cache`。
- **🔄 账号迁移与防封中心**：可视化执行新旧账号数据无损合并迁移。

---

## 🔄 三、登录新账号后，如何将旧账号记录 1:1 挂载回 Claude Desktop 原生界面？

当你退出被封禁的账号并在 Claude Desktop 登录一个**新账号**后，客户端会创建一个新的 `<accountUuid>/<orgUuid>` 目录。你可以通过 Web 工作台的 **“🔄 账号迁移与防封中心”** 页面一键迁移，或在终端运行：

```bash
# 1. 查看本机已发现的所有新旧账号 <accountUuid>/<orgUuid>
python3 migrate_account.py --list

# 2. 模拟预检 (Dry Run，不实际写入文件)
python3 migrate_account.py --to <新账号UUID>/<新组织UUID> --dry-run

# 3. 执行无损合并迁移（将所有历史账号下的 Code 会话、Cowork 空间 spaces.json、.project-cache 与会话目录合并至新账号）
python3 migrate_account.py --to <新账号UUID>/<新组织UUID>
```

迁移完成后重启 Claude Desktop，被封禁账号下的所有历史项目文件夹、Code 会话和 Cowork 空间即可原生显示在新账号的侧边栏中！

---

## ⏰ 四、团队日常防封号持续自动备份 (`auto_backup.py`)

为了防止 Chromium 缓存因容量上限淘汰旧的云端对话，建议所有使用 Claude 的开发人员开启后台定时增量备份：

```bash
# 立即执行一次防清理加固 + 增量备份
python3 auto_backup.py --run-once

# (macOS) 注册 LaunchAgent 后台任务，每 30 分钟自动增量备份一次
python3 auto_backup.py --install-launchd
launchctl load -w ~/Library/LaunchAgents/com.internal.claude-antiban-backup.plist
```

---

## 🧪 五、运行自动化测试

本仓库包含完整的脱敏合成单元测试（验证 Snappy 解压、Simple Cache CRC32 校验、JSONL 多轮对话解析、代码 `Write`/`Edit` 逆向重构以及跨账号迁移）：

```bash
python3 -m unittest discover -s tests -v
```

---

## 🔒 六、隐私与安全说明

- **纯本地离线运行**：所有解析、重构与 Web 预览均在本地 `127.0.0.1` 完成，绝不发起任何外部网络请求。
- **自动防泄漏保护**：`.gitignore` 已默认屏蔽 `recovered_data/`、`backups/`、`private/` 及所有 `*.log` 文件，确保个人对话与项目代码不会被误提交到 Git 仓库。
