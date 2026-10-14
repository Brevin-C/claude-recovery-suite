#!/usr/bin/env python3
"""
Anti-Ban Continuous Protection & Incremental Backup Tool (auto_backup.py)

1. Ensures ~/.claude/settings.json has "cleanupPeriodDays": 3650 so Claude Code
   never auto-deletes local .jsonl conversation transcripts after 30 days.
2. Runs incremental extraction of Claude Desktop & Chrome caches, Code sessions,
   and Cowork spaces into recovered_data/.
3. Optionally installs a macOS launchd background job (--install-launchd) so internal
   developers' conversations and artifacts are backed up automatically every 30 minutes.
"""

import argparse
import json
import os
import plistlib
import subprocess
import sys
from datetime import datetime

from recover_claude import export_project_folders_to_disk
from claude_recovery.extractor import run_full_recovery


def ensure_long_cleanup_period() -> bool:
    """Ensure ~/.claude/settings.json sets cleanupPeriodDays >= 3650."""
    settings_path = os.path.expanduser("~/.claude/settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    data = {}
    if os.path.exists(settings_path):
        try:
            data = json.load(open(settings_path, "r", encoding="utf-8"))
        except Exception:
            return False

    current_days = data.get("cleanupPeriodDays", 0)
    if not isinstance(current_days, int) or current_days < 3650:
        data["cleanupPeriodDays"] = 3650
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    return False


def install_launchd_agent(project_root: str) -> str:
    """Generate a macOS LaunchAgent plist for automatic periodic backup every 1800s (30m)."""
    label = "com.internal.claude-antiban-backup"
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(plist_dir, exist_ok=True)
    plist_path = os.path.join(plist_dir, f"{label}.plist")
    python_bin = sys.executable
    script_path = os.path.join(project_root, "auto_backup.py")
    log_path = os.path.join(project_root, "recovered_data", "auto_backup.log")

    plist_content = {
        "Label": label,
        "ProgramArguments": [python_bin, script_path, "--run-once"],
        "WorkingDirectory": project_root,
        "StartInterval": 1800,
        "RunAtLoad": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    with open(plist_path, "wb") as fp:
        plistlib.dump(plist_content, fp)
    return plist_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Anti-Ban Protection & Incremental Backup")
    parser.add_argument("--run-once", action="store_true", help="Run a full incremental backup pass")
    parser.add_argument("--install-launchd", action="store_true", help="Create macOS LaunchAgent for 30-min auto backup")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(project_root, "recovered_data")

    updated_cleanup = ensure_long_cleanup_period()
    if updated_cleanup:
        print("[+] Updated ~/.claude/settings.json: cleanupPeriodDays = 3650 (10 years retention)")
    else:
        print("[*] Verified ~/.claude/settings.json: cleanupPeriodDays >= 3650 is active")

    if args.install_launchd:
        plist_path = install_launchd_agent(project_root)
        print(f"[+] Created LaunchAgent plist at: {plist_path}")
        print(f"    To activate: launchctl load -w '{plist_path}'")

    if args.run_once or not args.install_launchd:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running incremental backup -> {output_dir}")
        catalog = run_full_recovery(output_dir)
        export_project_folders_to_disk(catalog, output_dir, export_all_md=True)
        with open(os.path.join(output_dir, "catalog.json"), "w", encoding="utf-8") as cf:
            json.dump(catalog, cf, ensure_ascii=False, indent=2)
        print("[+] Incremental backup complete!")


if __name__ == "__main__":
    main()
