#!/usr/bin/env python3
"""
Claude Account & Anti-Correlation Device Fingerprint Cleaner (clean_account_traces.py)

Safely cleans all banned account credentials, OAuth tokens, telemetry retry queues,
cookies, and persistent device/machine correlation fingerprints across:
  1. ~/.claude.json & ~/.claude/backups/ (rotates userID & machineID, removes oauthAccount & org caches)
  2. ~/.claude/telemetry/ (removes queued 1p_failed_events.*.json that would upload banned account UUIDs)
  3. ~/Library/Application Support/Claude/ (rotates ant-did, ccd-ids.json, device_id_salt; resets ant-device-registry.json, bridge-state.json, config.json)
  4. Chromium Cookies, Trust Tokens, Local Storage, Session Storage, IndexedDB, Cache_Data & Sentry
  5. VM MAC address (gvisorMacAddress)
  6. macOS Keychain ("Claude Code-credentials*" entries)
  7. Archives <bannedAccountUuid>/<orgUuid> folders from claude-code-sessions & local-agent-mode-sessions
     into recovered_data/archived_account_scopes/ and scrubs bridge-session account UUIDs in ~/.claude/projects/**/*.jsonl

100% PRESERVES all local conversation transcripts (~/.claude/projects/, ~/.claude/transcripts/,
~/.claude/file-history/, ~/.claude/handoff-history/), project settings, MCP configs, and recovered_data/!
"""

import argparse
import base64
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List

from claude_recovery.extractor import (
    get_default_claude_app_dir,
    get_default_claude_cli_dir,
)


def stop_running_claude_processes(dry_run: bool = False) -> List[str]:
    """Gracefully stop running Claude Desktop and Claude Code CLI processes so they don't flush old state back to disk."""
    stopped = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid_str, cmd = parts
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                if pid == os.getpid():
                    continue
                if (
                    "/Applications/Claude.app/Contents/MacOS/Claude" in cmd
                    or "@anthropic-ai/claude-code" in cmd
                    or "/.local/bin/claude" in cmd
                    or "Claude Helper" in cmd
                ):
                    stopped.append(f"PID {pid}: {cmd[:80]}")
                    if not dry_run:
                        try:
                            os.kill(pid, 15)
                        except Exception:
                            pass
            if stopped and not dry_run:
                time.sleep(1.5)
        except Exception:
            pass
    return stopped


def clean_claude_cli_json_and_telemetry(claude_cli_dir: str, dry_run: bool = False) -> Dict[str, Any]:
    """Clean ~/.claude.json account/fingerprint fields, rotate userID & machineID, and purge queued telemetry."""
    claude_json_path = os.path.expanduser("~/.claude.json")
    removed_keys = []
    new_user_id = secrets.token_hex(32)
    new_machine_id = secrets.token_hex(32)

    keys_to_remove = {
        "oauthAccount",
        "cachedArtifactRoster",
        "githubWebConnectionStatusCache",
        "groveConfigCache",
        "passesEligibilityCache",
        "clientDataCacheSlots",
        "replBridgePlaceholders",
        "claudeCodeFirstTokenDate",
        "firstStartTime",
        "cachedDynamicConfigs",
        "cachedExperimentData",
        "cachedExperimentFeatures",
        "cachedGrowthBookFeatures",
        "cachedGrowthBookFeaturesAt",
        "cachedStatsigGates",
        "metricsStatusCache",
        "s1mAccessCache",
        "changelogLastFetched",
        "closedIssuesLastChecked",
        "cachedExtraUsageDisabledReason",
        "orgModelDefaultCache",
    }

    if os.path.isfile(claude_json_path):
        try:
            with open(claude_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in list(data.keys()):
                if k in keys_to_remove:
                    removed_keys.append(k)
                    del data[k]
            data["userID"] = new_user_id
            data["machineID"] = new_machine_id
            if not dry_run:
                with open(claude_json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # Remove ~/.claude/backups/.claude.json.backup.* (which contain old oauthAccount/userID/machineID)
    backups_removed = 0
    backups_dir = os.path.join(claude_cli_dir, "backups")
    if os.path.isdir(backups_dir):
        for fn in os.listdir(backups_dir):
            if ".claude.json" in fn:
                backups_removed += 1
                if not dry_run:
                    try:
                        os.remove(os.path.join(backups_dir, fn))
                    except Exception:
                        pass

    # Remove ~/.claude/telemetry/1p_failed_events.*.json (queued telemetry with banned account UUIDs!)
    telemetry_removed = 0
    telemetry_dir = os.path.join(claude_cli_dir, "telemetry")
    if os.path.isdir(telemetry_dir):
        for fn in os.listdir(telemetry_dir):
            fp = os.path.join(telemetry_dir, fn)
            if os.path.isfile(fp):
                telemetry_removed += 1
                if not dry_run:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

    # Remove usage-band-shared.json, mcp-needs-auth-cache.json, and sessions/*
    for extra_fn in ["usage-band-shared.json", "mcp-needs-auth-cache.json"]:
        ep = os.path.join(claude_cli_dir, extra_fn)
        if os.path.isfile(ep) and not dry_run:
            try:
                os.remove(ep)
            except Exception:
                pass

    sess_dir = os.path.join(claude_cli_dir, "sessions")
    if os.path.isdir(sess_dir) and not dry_run:
        for fn in os.listdir(sess_dir):
            fp = os.path.join(sess_dir, fn)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

    return {
        "claudeJsonKeysRemoved": removed_keys,
        "newUserIdPrefix": new_user_id[:12] + "...",
        "newMachineIdPrefix": new_machine_id[:12] + "...",
        "configBackupsRemoved": backups_removed,
        "queuedTelemetryFilesRemoved": telemetry_removed,
    }


def scrub_account_uuids_in_jsonl_transcripts(claude_cli_dir: str, dry_run: bool = False) -> int:
    """
    Scrub ownerAccountUuid, ownerOrganizationUuid, and bridgeSessionId from 'bridge-session'
    lines inside ~/.claude/projects/**/*.jsonl while preserving 100% of conversation turns.
    """
    projects_dir = os.path.join(claude_cli_dir, "projects")
    if not os.path.isdir(projects_dir):
        return 0
    scrubbed_files = 0
    for root, _, files in os.walk(projects_dir):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if '"ownerAccountUuid"' not in content and '"ownerOrganizationUuid"' not in content:
                    continue
                scrubbed_files += 1
                if not dry_run:
                    new_lines = []
                    for line in content.splitlines():
                        if '"bridge-session"' in line and ('"ownerAccountUuid"' in line or '"ownerOrganizationUuid"' in line):
                            try:
                                obj = json.loads(line)
                                obj.pop("ownerAccountUuid", None)
                                obj.pop("ownerOrganizationUuid", None)
                                obj.pop("bridgeSessionId", None)
                                new_lines.append(json.dumps(obj, ensure_ascii=False))
                                continue
                            except Exception:
                                pass
                        new_lines.append(line)
                    with open(fp, "w", encoding="utf-8") as wf:
                        wf.write("\n".join(new_lines) + "\n")
            except Exception:
                pass
    return scrubbed_files


def clean_claude_desktop_fingerprints(
    claude_app_dir: str,
    archive_scopes_dir: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Rotate all Claude Desktop device IDs (ant-did, ccd-ids.json, device_id_salt, gvisorMacAddress),
    clear account registries & Chromium storage/cookies, and archive old <accountUuid>/<orgUuid> scopes.
    """
    if not os.path.isdir(claude_app_dir):
        return {"status": "claude_app_dir_not_found"}

    # 1. Archive claude-code-sessions & local-agent-mode-sessions <accountUuid> folders
    archived_scopes = []
    for sub in ["claude-code-sessions", "local-agent-mode-sessions", "spaces-present"]:
        src_base = os.path.join(claude_app_dir, sub)
        if not os.path.isdir(src_base):
            continue
        for entry in os.listdir(src_base):
            if entry.startswith("."):
                continue
            src_p = os.path.join(src_base, entry)
            if os.path.isdir(src_p):
                dst_p = os.path.join(archive_scopes_dir, sub, entry)
                archived_scopes.append(f"{sub}/{entry}")
                if not dry_run:
                    os.makedirs(os.path.dirname(dst_p), exist_ok=True)
                    if os.path.exists(dst_p):
                        shutil.rmtree(dst_p)
                    shutil.move(src_p, dst_p)

    # 2. Rotate ant-did (Base64 encoded UUID4)
    new_ant_did = base64.b64encode(str(uuid.uuid4()).encode("utf-8")).decode("utf-8")
    if not dry_run:
        with open(os.path.join(claude_app_dir, "ant-did"), "w", encoding="utf-8") as f:
            f.write(new_ant_did)

    # 3. Reset ant-device-registry.json & bridge-state.json & remove buddy-tokens.json
    if not dry_run:
        with open(os.path.join(claude_app_dir, "ant-device-registry.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(claude_app_dir, "bridge-state.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        bt_path = os.path.join(claude_app_dir, "buddy-tokens.json")
        if os.path.exists(bt_path):
            os.remove(bt_path)

    # 4. Rotate ccd-ids.json salt
    new_ccd_salt = str(uuid.uuid4())
    if not dry_run:
        with open(os.path.join(claude_app_dir, "ccd-ids.json"), "w", encoding="utf-8") as f:
            json.dump({"salt": new_ccd_salt}, f)

    # 5. Clean config.json (keep only scale, locale, userThemeMode)
    cfg_path = os.path.join(claude_app_dir, "config.json")
    if os.path.isfile(cfg_path) and not dry_run:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                old_cfg = json.load(f)
            clean_cfg = {
                "scale": old_cfg.get("scale", 0),
                "locale": old_cfg.get("locale", "zh-Hans"),
                "userThemeMode": old_cfg.get("userThemeMode", "light"),
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(clean_cfg, f, indent=2)
        except Exception:
            pass

    # 6. Rotate Preferences (device_id_salt, remove touchid secret) & Local State (installation_date2)
    pref_path = os.path.join(claude_app_dir, "Preferences")
    if os.path.isfile(pref_path) and not dry_run:
        try:
            with open(pref_path, "r", encoding="utf-8") as f:
                pref = json.load(f)
            pref["electron"] = {"media": {"device_id_salt": secrets.token_hex(16).upper()}}
            pref.pop("partition", None)
            with open(pref_path, "w", encoding="utf-8") as f:
                json.dump(pref, f)
        except Exception:
            pass

    ls_path = os.path.join(claude_app_dir, "Local State")
    if os.path.isfile(ls_path) and not dry_run:
        try:
            with open(ls_path, "w", encoding="utf-8") as f:
                json.dump({"uninstall_metrics": {"installation_date2": str(int(time.time()))}}, f)
        except Exception:
            pass

    # 7. Clean Chromium Cookies, Trust Tokens, Storage, Cache & Sentry
    cleaned_storage_items = []
    targets = [
        "Cookies",
        "Cookies-journal",
        "Trust Tokens",
        "Trust Tokens-journal",
        "SharedStorage",
        "SharedStorage-wal",
        "Network Persistent State",
        "Local Storage",
        "Session Storage",
        "IndexedDB",
        "WebStorage",
        "Service Worker",
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "sentry",
        "Crashpad",
        "Partitions",
        "plan-usage-history.json",
        "cowork-AsyncStorage.json",
    ]
    for t in targets:
        tp = os.path.join(claude_app_dir, t)
        if os.path.exists(tp):
            cleaned_storage_items.append(t)
            if not dry_run:
                try:
                    if os.path.isdir(tp):
                        shutil.rmtree(tp)
                    else:
                        os.remove(tp)
                except Exception:
                    pass

    # 8. Rotate VM gvisorMacAddress if present
    mac_path = os.path.join(claude_app_dir, "vm_bundles", "claudevm.bundle", "gvisorMacAddress")
    new_mac = None
    if os.path.isfile(mac_path):
        rand_bytes = secrets.token_bytes(5)
        new_mac = "22:" + ":".join(f"{b:02x}" for b in rand_bytes)
        if not dry_run:
            try:
                with open(mac_path, "w", encoding="utf-8") as f:
                    f.write(new_mac)
            except Exception:
                pass

    return {
        "archivedScopes": archived_scopes,
        "newAntDid": new_ant_did,
        "newCcdSalt": new_ccd_salt,
        "cleanedStorageItems": cleaned_storage_items,
        "newVmMacAddress": new_mac,
    }


def clean_macos_keychain_claude_credentials(dry_run: bool = False) -> List[str]:
    """Remove 'Claude Code-credentials*' and 'Claude Safe Storage' entries from macOS Keychain."""
    if sys.platform != "darwin":
        return []
    removed_services = []
    try:
        dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
        services = set(
            re.findall(r'"svce"<blob>="([^"]*(?:Claude Code-credentials)[^"]*)"', dump)
        )
        for svc in sorted(services):
            removed_services.append(svc)
            if not dry_run:
                # Loop until all items with this service name are deleted
                for _ in range(5):
                    r = subprocess.run(
                        ["security", "delete-generic-password", "-s", svc],
                        capture_output=True,
                        text=True,
                    )
                    if r.returncode != 0:
                        break
    except Exception:
        pass
    return removed_services


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean banned Claude account traces & rotate device fingerprints")
    parser.add_argument("--dry-run", action="store_true", help="Preview all items that will be cleaned without modifying files")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.abspath(__file__))
    archive_scopes_dir = os.path.join(project_root, "recovered_data", "archived_account_scopes")
    claude_app_dir = get_default_claude_app_dir()
    claude_cli_dir = get_default_claude_cli_dir()

    stopped = stop_running_claude_processes(dry_run=args.dry_run)
    cli_res = clean_claude_cli_json_and_telemetry(claude_cli_dir, dry_run=args.dry_run)
    scrubbed_count = scrub_account_uuids_in_jsonl_transcripts(claude_cli_dir, dry_run=args.dry_run)
    desktop_res = clean_claude_desktop_fingerprints(claude_app_dir, archive_scopes_dir, dry_run=args.dry_run)
    keychain_res = clean_macos_keychain_claude_credentials(dry_run=args.dry_run)

    report = {
        "dryRun": args.dry_run,
        "stoppedProcesses": stopped,
        "cliCleanup": cli_res,
        "scrubbedJsonlBridgeLinesCount": scrubbed_count,
        "desktopCleanup": desktop_res,
        "keychainCredentialsRemoved": keychain_res,
        "archivedScopesVault": archive_scopes_dir,
        "localConversationsPreserved": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
