#!/usr/bin/env python3
"""
CLI entrypoint to run 1:1 local Claude data recovery and export organized project folders.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict

from claude_recovery.extractor import run_full_recovery
from claude_recovery.transcript_parser import (
    parse_jsonl_full,
    render_conversation_to_markdown,
)


def safe_dirname(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", name.strip())
    return s[:80].strip("_.") or "Project"


def export_project_folders_to_disk(catalog: Dict[str, Any], output_dir: str, export_all_md: bool = True) -> Dict[str, int]:
    """
    Create a 1:1 physical folder structure under recovered_data/projects/<Project_Name>/
    containing:
    - project_info.json (paths, worktrees, PRs, artifacts, session list)
    - memory/ (recovered MEMORY.md & topic notes)
    - conversations/ (Markdown transcripts of multi-turn conversations)
    - reconstructed_code/ (code files extracted from Write/Edit tool calls for sessions)
    """
    projects_root = os.path.join(output_dir, "projects")
    os.makedirs(projects_root, exist_ok=True)

    exported_projects = 0
    exported_mds = 0
    reconstructed_files_count = 0

    used_dir_names: Dict[str, int] = {}

    for proj in catalog.get("codeProjects", []):
        pname = proj.get("projectName") or "Project"
        base_dname = safe_dirname(pname)
        cnt = used_dir_names.get(base_dname, 0)
        used_dir_names[base_dname] = cnt + 1
        dname = base_dname if cnt == 0 else f"{base_dname}_{cnt+1}"

        pdir = os.path.join(projects_root, dname)
        os.makedirs(pdir, exist_ok=True)
        exported_projects += 1

        # 1. Save project_info.json
        info_path = os.path.join(pdir, "project_info.json")
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "projectName": proj.get("projectName"),
                    "projectPath": proj.get("projectPath"),
                    "existsOnDisk": proj.get("existsOnDisk"),
                    "inGuiSidebar": proj.get("inGuiSidebar"),
                    "sessionCount": proj.get("sessionCount"),
                    "transcriptCount": proj.get("transcriptCount"),
                    "worktrees": proj.get("worktrees"),
                    "prLinks": proj.get("prLinks"),
                    "frameLinks": proj.get("frameLinks"),
                    "lastUpdatedFormatted": proj.get("lastUpdatedFormatted"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        # 2. Save memory files if any
        memories = proj.get("memories", [])
        if memories:
            mem_out = os.path.join(pdir, "memory")
            os.makedirs(mem_out, exist_ok=True)
            for m in memories:
                src = m.get("path")
                if src and os.path.exists(src):
                    try:
                        shutil.copy2(src, os.path.join(mem_out, m["name"]))
                    except Exception:
                        pass

        # 3. Export conversations to Markdown if export_all_md is True
        if export_all_md and proj.get("transcriptCount", 0) > 0:
            conv_dir = os.path.join(pdir, "conversations")
            os.makedirs(conv_dir, exist_ok=True)
            for sess in proj.get("sessions", []):
                tpath = sess.get("transcriptPath")
                if not tpath or not os.path.exists(tpath):
                    continue
                # Skip huge >30MB raw jsonl during bulk startup export if already accessible via Web GUI,
                # or export them cleanly
                dt_prefix = (sess.get("updatedFormatted") or "unknown")[:10]
                stitle = safe_dirname(sess.get("title") or sess.get("id") or "session")[:50]
                sid_short = (sess.get("id") or "00000000")[-8:]
                md_file = os.path.join(conv_dir, f"{dt_prefix}_{stitle}_{sid_short}.md")
                sess["exportedMdPath"] = md_file
                if not os.path.exists(md_file):
                    try:
                        parsed = parse_jsonl_full(tpath)
                        md_text = render_conversation_to_markdown(parsed, sess)
                        with open(md_file, "w", encoding="utf-8") as mf:
                            mf.write(md_text)
                        exported_mds += 1

                        # Reconstruct any code files that no longer exist at their original path on disk,
                        # and always save a manifest of written/edited files
                        if parsed.get("reconstructedFiles"):
                            rcode_dir = os.path.join(pdir, "reconstructed_code")
                            os.makedirs(rcode_dir, exist_ok=True)
                            for rf in parsed["reconstructedFiles"]:
                                if rf.get("content") and (not proj.get("existsOnDisk") or not rf.get("existsOnDisk")):
                                    rf_name = safe_dirname(rf.get("fileName") or "file.txt")
                                    with open(os.path.join(rcode_dir, rf_name), "w", encoding="utf-8") as wf:
                                        wf.write(rf["content"])
                                    reconstructed_files_count += 1
                    except Exception:
                        pass

    return {
        "exportedProjects": exported_projects,
        "exportedMarkdowns": exported_mds,
        "reconstructedCodeFiles": reconstructed_files_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude 1:1 Data Recovery & Anti-Ban Backup Tool")
    parser.add_argument(
        "--output",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovered_data"),
        help="Output directory for recovered data",
    )
    parser.add_argument(
        "--skip-bulk-md",
        action="store_true",
        help="Only build catalog and cloud/cowork exports without bulk rendering all 1,400+ Code JSONLs to .md",
    )
    args = parser.parse_args()

    print(f"[*] Starting Claude 1:1 Forensic Data Recovery -> {args.output}")
    catalog = run_full_recovery(args.output)
    s = catalog["summary"]
    print(f"[+] Catalog generated in {catalog['elapsedSeconds']}s:")
    print(f"    - Discovered Accounts/Orgs: {len(s['accountsDiscovered'])}")
    for acc in s["accountsDiscovered"]:
        print(
            f"      * Account {acc['accountUuid']} / Org {acc['orgUuid']}: "
            f"{acc['codeSessionCount']} Code GUI sessions, "
            f"{acc['coworkSessionCount']} Cowork sessions, "
            f"{acc['spacesCount']} Cowork spaces"
        )
    print(f"    - Code Project Folders:     {s['totalCodeProjects']}")
    print(f"    - Code Sessions Total:      {s['totalCodeSessions']} ({s['totalCodeTranscripts']} with full JSONL transcripts, {s['totalCodeTranscriptSizeMB']} MB)")
    print(f"    - Cowork Spaces & Sessions: {s['totalCoworkSpaces']} spaces, {s['totalCoworkSessions']} sessions, {s['totalCoworkOutputFiles']} output files")
    print(f"    - Cloud Cache Recovered:    {s['totalCloudConversations']} full cloud chats, {s['totalCloudProjects']} cloud projects, {s['totalCloudSandboxFiles']} sandbox/preview files")
    print(f"    - Git Worktrees & Snapshots:{s['totalWorktrees']} worktrees, {s['totalFileHistorySessions']} file-history sessions")

    if not args.skip_bulk_md:
        print("[*] Exporting 1:1 Project Folder Hierarchy & Markdown Conversations...")
        t1 = time.time()
        stats = export_project_folders_to_disk(catalog, args.output, export_all_md=True)
        # Update catalog with exportedMdPath references
        with open(os.path.join(args.output, "catalog.json"), "w", encoding="utf-8") as cf:
            json.dump(catalog, cf, ensure_ascii=False, indent=2)
        print(
            f"[+] Exported {stats['exportedProjects']} project folders, "
            f"{stats['exportedMarkdowns']} Markdown transcripts, and "
            f"{stats['reconstructedCodeFiles']} reconstructed code files in {round(time.time() - t1, 2)}s!"
        )


if __name__ == "__main__":
    main()
