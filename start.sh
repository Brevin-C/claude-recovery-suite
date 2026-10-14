#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

PORT="${1:-8765}"

echo "============================================================"
echo " 🛡️  Claude 1:1 本地数据复原与防封重放工作台 (一键启动)"
echo "============================================================"

# 1. 自动加固 ~/.claude/settings.json 的保留期为 3650 天 (防止 30 天自动清理)
python3 -c '
from auto_backup import ensure_long_cleanup_period
if ensure_long_cleanup_period():
    print("[+] 已自动将 ~/.claude/settings.json 的 cleanupPeriodDays 设为 3650 天 (防止历史记录被自动清理)")
else:
    print("[*] 已验证 ~/.claude/settings.json 的 cleanupPeriodDays >= 3650 天")
'

# 2. 如果尚未生成本地恢复索引，则自动执行全盘取证恢复与导出
if [ ! -f "recovered_data/catalog.json" ]; then
    echo "[*] 首次运行：正在扫描本机 5 层 Claude 存储并 1:1 复原项目文件夹与对话记录..."
    python3 recover_claude.py
else
    echo "[*] 检测到已有恢复归档 recovered_data/catalog.json (可在 Web 界面点击“⚡ 增量同步扫描”随时刷新)"
fi

# 3. 尝试自动打开浏览器并启动本地 Web 工作台
(sleep 1.2 && open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true) &
python3 server.py --port "${PORT}"
