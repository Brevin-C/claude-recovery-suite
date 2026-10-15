#!/usr/bin/env python3
"""
Claude Desktop Account Migration & Rebinding Tool (migrate_account.py)

When a Claude account is banned, logging into a new account in Claude Desktop
creates a new <accountUuid>/<orgUuid> namespace under:
  - ~/Library/Application Support/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/
  - ~/Library/Application Support/Claude/local-agent-mode-sessions/<accountUuid>/<orgUuid>/

This tool non-destructively merges all historical Code sessions, Cowork spaces (spaces.json),
.project-cache, and Cowork sessions from all previous/banned accounts into the active
(or specified) target account/org directory so they appear natively inside Claude Desktop GUI.
"""

import argparse
import glob
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from claude_recovery.extractor import get_default_claude_app_dir


def _get_search_roots(claude_app_dir: str) -> List[str]:
    roots = [claude_app_dir]
    if os.path.abspath(claude_app_dir) == os.path.abspath(get_default_claude_app_dir()):
        project_root = os.path.dirname(os.path.abspath(__file__))
        vault = os.path.join(project_root, "recovered_data", "archived_account_scopes")
        if os.path.isdir(vault) and vault not in roots:
            roots.append(vault)
    return roots


def discover_claude_desktop_scopes(claude_app_dir: str) -> List[Dict[str, Any]]:
    scopes: Dict[str, Dict[str, Any]] = {}

    for root_dir in _get_search_roots(claude_app_dir):
        for sub, field in [
            ("claude-code-sessions", "codeSessions"),
            ("local-agent-mode-sessions", "coworkSessions"),
        ]:
            base = os.path.join(root_dir, sub)
            if not os.path.isdir(base):
                continue
            for acc in os.listdir(base):
                acc_p = os.path.join(base, acc)
                if not os.path.isdir(acc_p) or acc.startswith("."):
                    continue
                for org in os.listdir(acc_p):
                    org_p = os.path.join(acc_p, org)
                    if not os.path.isdir(org_p) or org.startswith("."):
                        continue
                    key = f"{acc}/{org}"
                    entry = scopes.setdefault(
                        key,
                        {
                            "scopeKey": key,
                            "accountUuid": acc,
                            "orgUuid": org,
                            "sourceRoot": root_dir,
                            "codeSessions": 0,
                            "coworkSessions": 0,
                            "hasSpacesJson": False,
                            "hasProjectCache": False,
                            "lastModified": 0.0,
                        },
                    )
                    for fn in os.listdir(org_p):
                        fp = os.path.join(org_p, fn)
                        try:
                            mt = os.path.getmtime(fp)
                            if mt > entry["lastModified"]:
                                entry["lastModified"] = mt
                        except Exception:
                            pass
                        if fn.startswith("local_") and fn.endswith(".json"):
                            entry[field] += 1
                        elif fn == "spaces.json":
                            entry["hasSpacesJson"] = True
                        elif fn == ".project-cache":
                            entry["hasProjectCache"] = True

    res = list(scopes.values())
    for r in res:
        r["lastModifiedFormatted"] = (
            datetime.fromtimestamp(r["lastModified"]).strftime("%Y-%m-%d %H:%M:%S")
            if r["lastModified"] > 0
            else "Unknown"
        )
    res.sort(key=lambda x: x["lastModified"], reverse=True)
    return res


def detect_active_account_from_config(claude_app_dir: str) -> Optional[str]:
    cfg_path = os.path.join(claude_app_dir, "config.json")
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("lastKnownAccountUuid")
    except Exception:
        return None


def migrate_scopes_to_target(
    claude_app_dir: str,
    target_account_uuid: str,
    target_org_uuid: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Non-destructively copy/merge all claude-code-sessions and local-agent-mode-sessions
    from all other <accountUuid>/<orgUuid> scopes into target_account_uuid/target_org_uuid.
    """
    scopes = discover_claude_desktop_scopes(claude_app_dir)
    target_key = f"{target_account_uuid}/{target_org_uuid}"

    copied_code_sessions = 0
    copied_cowork_sessions = 0
    merged_spaces = 0
    copied_project_caches = 0

    target_ccs_dir = os.path.join(claude_app_dir, "claude-code-sessions", target_account_uuid, target_org_uuid)
    target_lam_dir = os.path.join(claude_app_dir, "local-agent-mode-sessions", target_account_uuid, target_org_uuid)

    if not dry_run:
        os.makedirs(target_ccs_dir, exist_ok=True)
        os.makedirs(target_lam_dir, exist_ok=True)

    # Load existing target spaces.json if present
    target_spaces_path = os.path.join(target_lam_dir, "spaces.json")
    target_spaces_list = []
    seen_space_ids = set()
    if os.path.exists(target_spaces_path):
        try:
            with open(target_spaces_path, "r", encoding="utf-8") as tf:
                target_spaces_list = json.load(tf).get("spaces", [])
            for s in target_spaces_list:
                if isinstance(s, dict) and s.get("id"):
                    seen_space_ids.add(s["id"])
        except Exception:
            pass

    for sc in scopes:
        if sc["scopeKey"] == target_key:
            continue
        src_acc = sc["accountUuid"]
        src_org = sc["orgUuid"]
        src_root = sc.get("sourceRoot") or claude_app_dir

        # 1. Copy claude-code-sessions JSONs
        src_ccs = os.path.join(src_root, "claude-code-sessions", src_acc, src_org)
        if os.path.isdir(src_ccs):
            for fn in os.listdir(src_ccs):
                if fn.endswith(".json"):
                    src_fp = os.path.join(src_ccs, fn)
                    dst_fp = os.path.join(target_ccs_dir, fn)
                    if not os.path.exists(dst_fp):
                        copied_code_sessions += 1
                        if not dry_run:
                            shutil.copy2(src_fp, dst_fp)

        # 2. Copy local-agent-mode-sessions (JSONs, session folders, spaces.json, .project-cache)
        src_lam = os.path.join(src_root, "local-agent-mode-sessions", src_acc, src_org)
        if os.path.isdir(src_lam):
            for fn in os.listdir(src_lam):
                src_fp = os.path.join(src_lam, fn)
                dst_fp = os.path.join(target_lam_dir, fn)

                if fn.startswith("local_") and fn.endswith(".json"):
                    if not os.path.exists(dst_fp):
                        copied_cowork_sessions += 1
                        if not dry_run:
                            shutil.copy2(src_fp, dst_fp)
                            # Also copy corresponding directory if present
                            src_sdir = os.path.join(src_lam, fn[:-5])
                            dst_sdir = os.path.join(target_lam_dir, fn[:-5])
                            if os.path.isdir(src_sdir) and not os.path.exists(dst_sdir):
                                shutil.copytree(src_sdir, dst_sdir)

                elif fn == "spaces.json" and os.path.isfile(src_fp):
                    try:
                        with open(src_fp, "r", encoding="utf-8") as sf_in:
                            sp_data = json.load(sf_in)
                        for sp in sp_data.get("spaces", []):
                            if isinstance(sp, dict) and sp.get("id") not in seen_space_ids:
                                seen_space_ids.add(sp.get("id"))
                                target_spaces_list.append(sp)
                                merged_spaces += 1
                    except Exception:
                        pass

                elif fn == ".project-cache" and os.path.isdir(src_fp):
                    dst_pc = os.path.join(target_lam_dir, ".project-cache")
                    if not dry_run:
                        os.makedirs(dst_pc, exist_ok=True)
                    for puid in os.listdir(src_fp):
                        s_pc = os.path.join(src_fp, puid)
                        d_pc = os.path.join(dst_pc, puid)
                        if os.path.isdir(s_pc) and not os.path.exists(d_pc):
                            copied_project_caches += 1
                            if not dry_run:
                                shutil.copytree(s_pc, d_pc)

    if merged_spaces > 0 and not dry_run:
        with open(target_spaces_path, "w", encoding="utf-8") as sf:
            json.dump({"spaces": target_spaces_list}, sf, ensure_ascii=False, indent=2)

    return {
        "targetScope": target_key,
        "dryRun": dry_run,
        "copiedCodeSessions": copied_code_sessions,
        "copiedCoworkSessions": copied_cowork_sessions,
        "mergedSpaces": merged_spaces,
        "copiedProjectCaches": copied_project_caches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate banned Claude Desktop sessions & spaces to a new account")
    parser.add_argument("--list", action="store_true", help="List all discovered account/org scopes on this machine")
    parser.add_argument("--to", metavar="ACCOUNT_UUID/ORG_UUID", help="Target accountUuid/orgUuid to migrate all sessions into")
    parser.add_argument("--dry-run", action="store_true", help="Preview migration without copying files")
    args = parser.parse_args()

    claude_app_dir = get_default_claude_app_dir()
    scopes = discover_claude_desktop_scopes(claude_app_dir)
    last_acc = detect_active_account_from_config(claude_app_dir)

    if args.list or not args.to:
        print("=== Discovered Claude Desktop Account/Organization Scopes ===")
        print(f"Last known account in config.json: {last_acc}")
        for idx, sc in enumerate(scopes, 1):
            print(
                f"  [{idx}] {sc['scopeKey']}\n"
                f"      - Code GUI Sessions: {sc['codeSessions']}\n"
                f"      - Cowork Sessions:   {sc['coworkSessions']}\n"
                f"      - Has spaces.json:   {sc['hasSpacesJson']}\n"
                f"      - Has .project-cache:{sc['hasProjectCache']}\n"
                f"      - Last Modified:     {sc['lastModifiedFormatted']}"
            )
        if not args.to:
            print("\nUsage to migrate all banned account sessions into a newly logged-in account:")
            print("  python3 migrate_account.py --to <newAccountUuid>/<newOrgUuid>")
            return

    if "/" not in args.to:
        print("[!] Error: --to must be in the format <accountUuid>/<orgUuid>")
        return

    target_acc, target_org = args.to.strip().split("/", 1)
    res = migrate_scopes_to_target(claude_app_dir, target_acc, target_org, dry_run=args.dry_run)
    print("=== Migration Summary ===")
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
