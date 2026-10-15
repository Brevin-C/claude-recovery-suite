#!/usr/bin/env python3
"""
Multi-layer forensic extractor and 1:1 reconstructor for Claude Desktop & Claude Code.
Recovers all projects, folder structures, multi-turn conversations, thinking blocks,
tool calls, git worktrees, shadow git repos, file-history snapshots, handoff docs,
Cowork spaces, and Chromium cached cloud chats/artifacts.
"""

import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import zlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from compression import zstd as _stdlib_zstd
except ImportError:
    _stdlib_zstd = None  # type: ignore

try:
    import zstandard as _ext_zstandard  # type: ignore
except ImportError:
    _ext_zstandard = None  # type: ignore


def decompress_zstd(data: bytes) -> bytes:
    """Decompress Zstandard frame across Python 3.9-3.14 (stdlib -> zstandard -> zstd CLI)."""
    if _stdlib_zstd is not None:
        return _stdlib_zstd.decompress(data)
    if _ext_zstandard is not None:
        return _ext_zstandard.ZstdDecompressor().decompress(data)
    p = subprocess.run(["zstd", "-d", "-q", "-c"], input=data, capture_output=True, timeout=10)
    if p.returncode == 0 and p.stdout:
        return p.stdout
    raise RuntimeError("Zstandard decompression unavailable")


def get_default_claude_app_dir() -> str:
    """Return platform-specific Claude Desktop application data directory."""
    env_dir = os.environ.get("CLAUDE_APP_DIR")
    if env_dir:
        return os.path.expanduser(env_dir)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(appdata, "Claude")
    return os.path.expanduser("~/.config/Claude")


def get_default_claude_cli_dir() -> str:
    """Return Claude Code CLI data directory (~/.claude)."""
    env_dir = os.environ.get("CLAUDE_CLI_DIR")
    if env_dir:
        return os.path.expanduser(env_dir)
    return os.path.expanduser("~/.claude")


def get_browser_cache_globs() -> List[str]:
    """Return Chromium Simple Cache glob patterns across macOS, Windows, and Linux."""
    if sys.platform == "darwin":
        return [
            os.path.expanduser("~/Library/Application Support/Google/Chrome/**/Cache_Data"),
            os.path.expanduser("~/Library/Application Support/Arc/**/Cache_Data"),
            os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser/**/Cache_Data"),
            os.path.expanduser("~/Library/Application Support/Microsoft Edge/**/Cache_Data"),
        ]
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return [
            os.path.join(local_app, "Google", "Chrome", "User Data", "**", "Cache_Data"),
            os.path.join(local_app, "Microsoft", "Edge", "User Data", "**", "Cache_Data"),
        ]
    return [
        os.path.expanduser("~/.cache/google-chrome/**/Cache_Data"),
        os.path.expanduser("~/.cache/chromium/**/Cache_Data"),
    ]

from .transcript_parser import (
    extract_worktree_name,
    fast_scan_jsonl_metadata,
    format_timestamp,
    normalize_base_project_path,
    parse_cloud_conversation,
    parse_jsonl_full,
    render_conversation_to_markdown,
)


def encode_cwd_for_claude_projects(cwd: str) -> str:
    """Replicate Claude Code's folder encoding: replace any non-alphanumeric ASCII char with '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# ============================================================================
# Layer 1: Pure-Python Snappy + LevelDB SSTable (.ldb) parser for Local Storage
# ============================================================================

def snappy_decompress(data: bytes) -> bytes:
    pos = 0
    n = len(data)
    res_len = 0
    shift = 0
    while pos < n:
        b = data[pos]
        pos += 1
        res_len |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    out = bytearray()
    while pos < n and len(out) < res_len:
        tag = data[pos]
        pos += 1
        t = tag & 0x03
        if t == 0:
            length = (tag >> 2) + 1
            if length > 60:
                extra = length - 60
                length = int.from_bytes(data[pos : pos + extra], "little") + 1
                pos += extra
            out.extend(data[pos : pos + length])
            pos += length
        else:
            if t == 1:
                length = ((tag >> 2) & 0x07) + 4
                offset = ((tag & 0xE0) << 3) | data[pos]
                pos += 1
            elif t == 2:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[pos : pos + 2], "little")
                pos += 2
            else:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[pos : pos + 4], "little")
                pos += 4
            if offset <= 0 or offset > len(out):
                break
            for _ in range(length):
                out.append(out[-offset])
    return bytes(out)


def _read_varint(buf: bytes, p: int) -> Tuple[int, int]:
    v = 0
    s = 0
    n = len(buf)
    while p < n:
        b = buf[p]
        p += 1
        v |= (b & 0x7F) << s
        if not (b & 0x80):
            break
        s += 7
    return v, p


def _decode_ls_str(b: bytes) -> str:
    if not b:
        return ""
    if b[0] == 0:
        return b[1:].decode("utf-16le", errors="ignore")
    if b[0] == 1:
        return b[1:].decode("latin1", errors="ignore")
    return b.decode("utf-8", errors="ignore")


def extract_leveldb_gui_state(claude_app_dir: str) -> Dict[str, Any]:
    """
    Extract GUI sidebar state (dframe-store, dframe-group-scopes, dframe-code-sections,
    ccd-session-store, auth:last-signout) from Claude Desktop Local Storage LevelDB.
    """
    state: Dict[str, Any] = {
        "sidebarProjectGroups": [],
        "pinnedGroups": {},
        "codeSections": {},
        "lastKnownMode": "code",
        "lastSidebarScopeKey": None,
        "selectedFolder": None,
        "lastForcedSignout": None,
        "rawKeysRecovered": 0,
    }
    ls_dir = os.path.join(claude_app_dir, "Local Storage", "leveldb")
    if not os.path.isdir(ls_dir):
        return state

    for fn in sorted(os.listdir(ls_dir)):
        if not fn.endswith(".ldb"):
            continue
        fpath = os.path.join(ls_dir, fn)
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
            if len(raw) < 48 or struct.unpack("<Q", raw[-8:])[0] != 0xDB4775248B80FB57:
                continue
            pos = len(raw) - 48
            _, pos = _read_varint(raw, pos)
            _, pos = _read_varint(raw, pos)
            io, pos = _read_varint(raw, pos)
            isz, pos = _read_varint(raw, pos)
            ib = snappy_decompress(raw[io : io + isz]) if raw[io + isz] == 1 else raw[io : io + isz]
            num_restarts = struct.unpack("<I", ib[-4:])[0]
            limit = len(ib) - 4 * (num_restarts + 1)
            p = 0
            while p < limit:
                _, p = _read_varint(ib, p)
                ns, p = _read_varint(ib, p)
                vl, p = _read_varint(ib, p)
                p += ns
                bo, vp = _read_varint(ib, p)
                bs, vp = _read_varint(ib, vp)
                p += vl
                blk = snappy_decompress(raw[bo : bo + bs]) if raw[bo + bs] == 1 else raw[bo : bo + bs]
                nr = struct.unpack("<I", blk[-4:])[0]
                blimit = len(blk) - 4 * (nr + 1)
                bp = 0
                last_key = b""
                while bp < blimit:
                    s, bp = _read_varint(blk, bp)
                    ns2, bp = _read_varint(blk, bp)
                    vl2, bp = _read_varint(blk, bp)
                    key = last_key[:s] + blk[bp : bp + ns2]
                    bp += ns2
                    val = blk[bp : bp + vl2]
                    bp += vl2
                    last_key = key
                    if len(key) < 8:
                        continue
                    vt = struct.unpack("<Q", key[-8:])[0] & 0xFF
                    if vt != 1 or not val:
                        continue
                    state["rawKeysRecovered"] += 1
                    ks = key[:-8].decode("utf-8", errors="ignore")
                    vs = _decode_ls_str(val)
                    if ks.endswith("dframe-store"):
                        try:
                            obj = json.loads(vs).get("state", {})
                            cgroups = obj.get("collapsedGroups", [])
                            state["sidebarProjectGroups"] = [
                                g[len("project-") :] if g.startswith("project-") else g
                                for g in cgroups
                                if g not in ("older", "project-__scratch__")
                            ]
                            state["lastKnownMode"] = obj.get("lastKnownMode", "code")
                            state["lastSidebarScopeKey"] = obj.get("lastSidebarScopeKey")
                        except Exception:
                            pass
                    elif ks.endswith("LSS-persisted.dframe-group-scopes"):
                        try:
                            state["pinnedGroups"] = json.loads(vs).get("value", {})
                        except Exception:
                            pass
                    elif ks.endswith("LSS-persisted.dframe-code-sections"):
                        try:
                            state["codeSections"] = json.loads(vs).get("value", {})
                        except Exception:
                            pass
                    elif ks.endswith("ccd-session-store"):
                        try:
                            state["selectedFolder"] = json.loads(vs).get("state", {}).get("selectedFolder")
                        except Exception:
                            pass
                    elif ks.endswith("auth:last-signout"):
                        try:
                            state["lastForcedSignout"] = json.loads(vs)
                        except Exception:
                            pass
        except Exception:
            continue

    return state


# ============================================================================
# Layer 4: Chromium Simple Cache (_0) Parser with CRC32 + Zstd/Gzip Decomp
# ============================================================================

EOF_BYTES = struct.pack("<Q", 0xF4FA6F45970D41D8)
HDR_MAGIC = 0xFCFB6D1BA7725C30


def extract_chromium_simple_cache_entry(fpath: str) -> Tuple[Optional[str], Optional[bytes], bool]:
    """
    Parse a Chromium Simple Cache *_0 file, verify Stream 1 CRC32, and decompress
    Zstandard (0xFD2FB528) or Gzip payloads.
    """
    with open(fpath, "rb") as f:
        data = f.read()
    if len(data) < 48:
        return None, None, False
    magic, version, key_len, key_hash, pad = struct.unpack("<QIIII", data[:24])
    if magic != HDR_MAGIC or key_len > len(data) - 24:
        return None, None, False
    key = data[24 : 24 + key_len].decode("utf-8", errors="ignore")
    if "claude.ai" not in key and "anthropic" not in key:
        return key, None, False
    s1_start = 24 + key_len
    pos = data.find(EOF_BYTES, s1_start)
    if pos == -1 or pos + 24 > len(data):
        return key, None, False
    eof_magic, flags, crc, stream_size, pad2 = struct.unpack("<QIIII", data[pos : pos + 24])
    s1_end = pos - 32 if (flags & 2) else pos
    if s1_end < s1_start:
        return key, None, False
    raw = data[s1_start:s1_end]
    crc_ok = (zlib.crc32(raw) & 0xFFFFFFFF) == crc
    body = raw
    if raw.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            body = decompress_zstd(raw)
        except Exception:
            pass
    elif raw.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(raw)
        except Exception:
            pass
    return key, body, crc_ok


def scan_all_chromium_caches(claude_app_dir: str, out_cloud_dir: str) -> Dict[str, Any]:
    """
    Scan Claude Desktop and browser Simple Cache directories to recover:
    - Full cloud chat_conversations (?tree=True&rendering_mode=messages...), including all response versions!
    - Cloud projects & conversation lists (projects_v2, chat_conversations_v2, docs, syncs)
    - Wiggle sandbox files (/wiggle/download-file?path=...) & file previews
    """
    cache_dirs = glob.glob(os.path.join(claude_app_dir, "**", "Cache_Data"), recursive=True)
    for pattern in get_browser_cache_globs():
        cache_dirs.extend(glob.glob(pattern, recursive=True))

    convs_by_uuid: Dict[str, Dict[str, Any]] = {}
    conv_versions_by_sha: Dict[str, Dict[str, Any]] = {}
    cloud_projects: Dict[str, Dict[str, Any]] = {}
    conv_list_summaries: Dict[str, Dict[str, Any]] = {}
    sandbox_files: List[Dict[str, Any]] = []

    os.makedirs(os.path.join(out_cloud_dir, "conversations_md"), exist_ok=True)
    os.makedirs(os.path.join(out_cloud_dir, "conversations_json"), exist_ok=True)
    os.makedirs(os.path.join(out_cloud_dir, "conversation_versions"), exist_ok=True)
    os.makedirs(os.path.join(out_cloud_dir, "sandbox_files"), exist_ok=True)

    seen_sandbox_hashes: Set[str] = set()

    for cdir in cache_dirs:
        if not os.path.isdir(cdir):
            continue
        for fname in os.listdir(cdir):
            if not fname.endswith("_0"):
                continue
            fpath = os.path.join(cdir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                key, body, crc_ok = extract_chromium_simple_cache_entry(fpath)
            except Exception:
                continue
            if not key or not body or "/api/" not in key:
                continue

            url = "https://" + key.split("https://", 1)[-1] if "https://" in key else key

            # 1. Full cloud chat_conversations
            if "/chat_conversations/" in url and ("tree=True" in url or "rendering_mode=messages" in url):
                try:
                    obj = json.loads(body)
                    if isinstance(obj, dict) and "uuid" in obj and "chat_messages" in obj:
                        uid = obj["uuid"]
                        body_sha = hashlib.sha256(body).hexdigest()
                        if body_sha not in conv_versions_by_sha:
                            ver_path = os.path.join(
                                out_cloud_dir, "conversation_versions", f"{uid[:8]}_{body_sha[:12]}.json"
                            )
                            with open(ver_path, "wb") as vf:
                                vf.write(body)
                            conv_versions_by_sha[body_sha] = {
                                "uuid": uid,
                                "sha256": body_sha,
                                "name": obj.get("name"),
                                "messageCount": len(obj.get("chat_messages", [])),
                                "updatedAt": obj.get("updated_at"),
                                "path": ver_path,
                            }
                        if uid not in convs_by_uuid or len(obj["chat_messages"]) >= len(
                            convs_by_uuid[uid].get("chat_messages", [])
                        ):
                            convs_by_uuid[uid] = obj
                except Exception:
                    pass

            # 2. Cloud conversation lists (chat_conversations_v2 / conversations_v2)
            elif "chat_conversations_v2" in url or "conversations_v2" in url:
                try:
                    obj = json.loads(body)
                    items = obj.get("data", []) if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
                    for item in items:
                        if isinstance(item, dict) and item.get("uuid"):
                            conv_list_summaries[item["uuid"]] = item
                except Exception:
                    pass

            # 3. Cloud projects metadata
            elif "/projects" in url:
                try:
                    obj = json.loads(body)
                    items = obj.get("data", []) if isinstance(obj, dict) and "data" in obj else (
                        obj if isinstance(obj, list) else [obj]
                    )
                    for item in items:
                        if isinstance(item, dict) and item.get("uuid") and item.get("name"):
                            puid = item["uuid"]
                            cloud_projects[puid] = {
                                "uuid": puid,
                                "name": item.get("name"),
                                "description": item.get("description", ""),
                                "promptTemplate": item.get("prompt_template", ""),
                                "createdAt": item.get("created_at"),
                                "updatedAt": item.get("updated_at"),
                                "isStarred": item.get("is_starred"),
                            }
                except Exception:
                    pass

            # 4. Wiggle sandbox files & file previews
            elif "wiggle/download-file" in url or ("/files/" in url and ("/preview" in url or "/thumbnail" in url or "/content" in url)):
                if len(body) < 350 and body.startswith(b"{") and b'"error"' in body:
                    continue
                sha = hashlib.sha256(body).hexdigest()[:16]
                if sha in seen_sandbox_hashes:
                    continue
                seen_sandbox_hashes.add(sha)

                parsed_url = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed_url.query)
                remote_path = qs.get("path", [None])[0]
                if remote_path:
                    base_name = os.path.basename(remote_path)
                else:
                    parts = [p for p in parsed_url.path.split("/") if p]
                    file_uuid = parts[-2] if len(parts) >= 2 else fname
                    ext = _guess_extension(body)
                    base_name = f"file_{file_uuid[:12]}{ext}"

                safe_name = re.sub(r'[\\/:*?"<>|]', "_", base_name)
                saved_name = f"{sha[:8]}_{safe_name}"
                saved_path = os.path.join(out_cloud_dir, "sandbox_files", saved_name)
                try:
                    with open(saved_path, "wb") as out_f:
                        out_f.write(body)
                except Exception:
                    pass

                conv_match = re.search(r"/conversations/([^/]+)/", url)
                org_match = re.search(r"/organizations/([^/]+)/", url) or re.search(r"/api/([0-9a-f-]{36})/files/", url)
                sandbox_files.append({
                    "id": sha,
                    "fileName": base_name,
                    "savedName": saved_name,
                    "savedPath": saved_path,
                    "remotePath": remote_path or parsed_url.path,
                    "url": url,
                    "size": len(body),
                    "crcVerified": crc_ok,
                    "conversationId": conv_match.group(1) if conv_match else None,
                    "orgUuid": org_match.group(1) if org_match else None,
                })

    # Merge .project-cache from local-agent-mode-sessions into cloud_projects
    pcache_patterns = [
        os.path.join(claude_app_dir, "local-agent-mode-sessions", "*", "*", ".project-cache", "*"),
        os.path.join(os.path.dirname(out_cloud_dir), "archived_account_scopes", "local-agent-mode-sessions", "*", "*", ".project-cache", "*"),
    ]
    for pat in pcache_patterns:
        for pcache in glob.glob(pat):
            if os.path.isdir(pcache):
                meta_p = os.path.join(pcache, "metadata.json")
                if os.path.exists(meta_p):
                    try:
                        with open(meta_p, "r", encoding="utf-8") as mf:
                            m = json.load(mf)
                        puid = m.get("uuid") or os.path.basename(pcache)
                        docs_dir = os.path.join(pcache, "docs")
                        files_dir = os.path.join(pcache, "files")
                        docs = os.listdir(docs_dir) if os.path.isdir(docs_dir) else []
                        files = os.listdir(files_dir) if os.path.isdir(files_dir) else []
                        cloud_projects[puid] = {
                            "uuid": puid,
                            "name": m.get("name", puid),
                            "description": m.get("description", ""),
                            "promptTemplate": m.get("prompt_template", ""),
                            "syncedAt": m.get("synced_at"),
                            "localCacheDir": pcache,
                            "docs": [os.path.join(docs_dir, d) for d in docs],
                            "files": [os.path.join(files_dir, f) for f in files],
                        }
                    except Exception:
                        pass

    # Export all 47+ full cloud conversations to JSON + Markdown
    cloud_conv_entries = []
    for uid, cobj in convs_by_uuid.items():
        parsed = parse_cloud_conversation(cobj)
        title = cobj.get("name") or "Untitled Cloud Chat"
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:60].strip("_") or "chat"
        dt_prefix = format_timestamp(cobj.get("updated_at") or cobj.get("created_at"))[:10] or "unknown"
        json_path = os.path.join(out_cloud_dir, "conversations_json", f"{dt_prefix}_{safe_title}_{uid[:8]}.json")
        md_path = os.path.join(out_cloud_dir, "conversations_md", f"{dt_prefix}_{safe_title}_{uid[:8]}.md")
        try:
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(cobj, jf, ensure_ascii=False, indent=2)
            md_content = render_conversation_to_markdown(
                parsed,
                {
                    "title": title,
                    "sessionId": uid,
                    "sourceType": "Claude Cloud Cache (Zstd Reconstructed)",
                    "projectPath": cloud_projects.get(cobj.get("project_uuid"), {}).get("name") or cobj.get("project_uuid"),
                    "model": cobj.get("model"),
                    "createdAt": cobj.get("created_at"),
                    "updatedAt": cobj.get("updated_at"),
                },
            )
            with open(md_path, "w", encoding="utf-8") as mf:
                mf.write(md_content)
        except Exception:
            pass

        cloud_conv_entries.append({
            "sessionId": uid,
            "title": title,
            "summary": cobj.get("summary", ""),
            "model": cobj.get("model"),
            "projectUuid": cobj.get("project_uuid"),
            "projectName": cloud_projects.get(cobj.get("project_uuid"), {}).get("name"),
            "createdAt": cobj.get("created_at"),
            "updatedAt": cobj.get("updated_at"),
            "updatedFormatted": format_timestamp(cobj.get("updated_at") or cobj.get("created_at")),
            "messageCount": len(cobj.get("chat_messages", [])),
            "userTurnCount": parsed["meta"]["userTurnCount"],
            "assistantTurnCount": parsed["meta"]["assistantTurnCount"],
            "jsonPath": json_path,
            "mdPath": md_path,
            "sourceType": "cloud_cache",
        })

    cloud_conv_entries.sort(key=lambda x: x.get("updatedFormatted") or "", reverse=True)

    return {
        "conversations": cloud_conv_entries,
        "conversationResponseVersions": list(conv_versions_by_sha.values()),
        "projects": list(cloud_projects.values()),
        "conversationListSummaries": list(conv_list_summaries.values()),
        "sandboxFiles": sandbox_files,
    }


def _guess_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".docx"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    if data.startswith(b"{") or data.startswith(b"["):
        return ".json"
    return ".bin"


# ============================================================================
# Auxiliary Evidence Scanners (file-history, handoff-history, git-shadow, etc.)
# ============================================================================

def scan_auxiliary_evidence(claude_cli_dir: str, claude_app_dir: str, global_backup_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Scan ~/.claude/file-history (with resolved original paths), ~/.claude/handoff-history,
    ~/.claude/paste-cache, ~/.claude/history.jsonl, ~/Library/Application Support/Claude/pending-uploads,
    and ~/Library/Application Support/Claude/git-shadow.
    """
    # 1. File-history snapshots (320 files across 27 sessions)
    fh_dir = os.path.join(claude_cli_dir, "file-history")
    resolved_snapshots: List[Dict[str, Any]] = []
    if os.path.isdir(fh_dir):
        for sid in sorted(os.listdir(fh_dir)):
            sdir = os.path.join(fh_dir, sid)
            if not os.path.isdir(sdir) or sid.startswith("."):
                continue
            for fn in sorted(os.listdir(sdir)):
                if fn.startswith("."):
                    continue
                fp = os.path.join(sdir, fn)
                if not os.path.isfile(fp):
                    continue
                binfo = global_backup_map.get(fn, {})
                resolved_snapshots.append({
                    "sessionId": sid,
                    "backupFileName": fn,
                    "snapshotPath": fp,
                    "size": os.path.getsize(fp),
                    "originalPath": binfo.get("originalPath") or f"[Unmapped Snapshot] {fn}",
                    "originalRelPath": binfo.get("originalRelPath") or fn,
                    "version": binfo.get("version"),
                    "backupTime": binfo.get("backupTime") or format_timestamp(os.path.getmtime(fp)),
                    "isMappedToOriginalPath": bool(binfo.get("originalPath")),
                })

    # 2. Handoff history (~/.claude/handoff-history)
    hh_dir = os.path.join(claude_cli_dir, "handoff-history")
    handoff_files: List[Dict[str, Any]] = []
    if os.path.isdir(hh_dir):
        for root, _, files in os.walk(hh_dir):
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                fp = os.path.join(root, fn)
                handoff_files.append({
                    "name": os.path.relpath(fp, hh_dir),
                    "path": fp,
                    "size": os.path.getsize(fp),
                    "mtime": format_timestamp(os.path.getmtime(fp)),
                })

    # 3. Paste cache (~/.claude/paste-cache)
    pc_dir = os.path.join(claude_cli_dir, "paste-cache")
    paste_files: List[Dict[str, Any]] = []
    if os.path.isdir(pc_dir):
        for fn in sorted(os.listdir(pc_dir)):
            fp = os.path.join(pc_dir, fn)
            if os.path.isfile(fp) and not fn.startswith("."):
                paste_files.append({
                    "name": fn,
                    "path": fp,
                    "size": os.path.getsize(fp),
                    "mtime": format_timestamp(os.path.getmtime(fp)),
                })

    # 4. Pending uploads (~/Library/Application Support/Claude/pending-uploads)
    pu_dir = os.path.join(claude_app_dir, "pending-uploads")
    pending_uploads: List[Dict[str, Any]] = []
    if os.path.isdir(pu_dir):
        for fn in sorted(os.listdir(pu_dir)):
            fp = os.path.join(pu_dir, fn)
            if os.path.isfile(fp) and not fn.startswith("."):
                pending_uploads.append({
                    "name": fn,
                    "path": fp,
                    "size": os.path.getsize(fp),
                    "mtime": format_timestamp(os.path.getmtime(fp)),
                })

    # 5. Shadow Git Repositories (~/Library/Application Support/Claude/git-shadow)
    gs_dir = os.path.join(claude_app_dir, "git-shadow")
    shadow_repos: List[Dict[str, Any]] = []
    if os.path.isdir(gs_dir):
        for fn in sorted(os.listdir(gs_dir)):
            p = os.path.join(gs_dir, fn)
            if not os.path.isdir(p) or fn.startswith("."):
                continue
            shadow_of_file = os.path.join(p, "shadow-of")
            shadow_target = None
            if os.path.isfile(shadow_of_file):
                try:
                    with open(shadow_of_file, "r", encoding="utf-8", errors="ignore") as sf:
                        shadow_target = sf.read().strip()
                except Exception:
                    pass
            commits = []
            try:
                r = subprocess.run(
                    ["git", "--no-pager", "-C", p, "log", "--all", "--oneline", "-n", "10"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if r.returncode == 0 and r.stdout.strip():
                    commits = r.stdout.strip().splitlines()
            except Exception:
                pass
            if shadow_target or commits:
                shadow_repos.append({
                    "id": fn,
                    "path": p,
                    "shadowOf": shadow_target,
                    "recentCommits": commits,
                })

    # 6. CLI Input History (~/.claude/history.jsonl)
    hist_path = os.path.join(claude_cli_dir, "history.jsonl")
    input_history_count = 0
    if os.path.isfile(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8", errors="ignore") as hf:
                input_history_count = sum(1 for line in hf if line.strip())
        except Exception:
            pass

    return {
        "fileHistorySnapshots": resolved_snapshots,
        "handoffFiles": handoff_files,
        "pasteCacheFiles": paste_files,
        "pendingUploads": pending_uploads,
        "shadowGitRepos": shadow_repos,
        "cliInputHistoryPath": hist_path if os.path.isfile(hist_path) else None,
        "cliInputHistoryCount": input_history_count,
    }


# ============================================================================
# Layer 2 & 3: Code Projects, Sessions, Worktrees & Cowork Spaces Recovery
# ============================================================================

def run_full_recovery(
    output_dir: str,
    claude_app_dir: Optional[str] = None,
    claude_cli_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute complete 1:1 recovery across all local Claude storage locations and
    build the structured recovery archive + master catalog.json.
    """
    t0 = time.time()
    claude_app_dir = claude_app_dir or get_default_claude_app_dir()
    claude_cli_dir = claude_cli_dir or get_default_claude_cli_dir()
    os.makedirs(output_dir, exist_ok=True)

    existing_catalog_path = os.path.join(output_dir, "catalog.json")
    prev_catalog: Dict[str, Any] = {}
    if os.path.isfile(existing_catalog_path):
        try:
            with open(existing_catalog_path, "r", encoding="utf-8") as ef:
                prev_catalog = json.load(ef)
        except Exception:
            prev_catalog = {}

    # 1. Extract LevelDB GUI sidebar state (or preserve previous if Local Storage was cleaned)
    gui_state = extract_leveldb_gui_state(claude_app_dir)
    if not gui_state.get("sidebarProjectGroups") and prev_catalog.get("guiState", {}).get("sidebarProjectGroups"):
        gui_state = prev_catalog["guiState"]

    pinned_session_map: Dict[str, str] = {}
    pinned_groups_list: List[Dict[str, Any]] = []
    for scope_key, scope_val in gui_state.get("pinnedGroups", {}).items():
        if not isinstance(scope_val, dict):
            continue
        gmap = {g["id"]: g["name"] for g in scope_val.get("groups", []) if isinstance(g, dict) and "id" in g}
        for k, gid in scope_val.get("assignments", {}).items():
            sid = k.split(":", 1)[-1] if ":" in k else k
            pinned_session_map[sid] = gmap.get(gid, gid)
        for gid, gname in gmap.items():
            order_list = [
                (x.split(":", 1)[-1] if ":" in x else x)
                for x in scope_val.get("order", {}).get(gid, [])
            ]
            pinned_groups_list.append({
                "id": gid,
                "name": gname,
                "scopeKey": scope_key,
                "sessionIds": order_list,
            })

    # 2. Load git-worktrees.json
    worktrees_map: Dict[str, Dict[str, Any]] = {}
    wt_file = os.path.join(claude_app_dir, "git-worktrees.json")
    if os.path.exists(wt_file):
        try:
            with open(wt_file, "r", encoding="utf-8") as wf:
                wt_data = json.load(wf)
            for wt_name, wt_info in wt_data.get("worktrees", {}).items():
                if isinstance(wt_info, dict):
                    worktrees_map[wt_name] = wt_info
        except Exception:
            pass

    # 3. Scan ~/.claude/file-history snapshots directory
    file_history_dir = os.path.join(claude_cli_dir, "file-history")
    file_history_map: Dict[str, List[str]] = {}
    if os.path.isdir(file_history_dir):
        for sid in os.listdir(file_history_dir):
            sdir = os.path.join(file_history_dir, sid)
            if os.path.isdir(sdir) and not sid.startswith("."):
                snaps = [os.path.join(sdir, f) for f in os.listdir(sdir) if not f.startswith(".")]
                if snaps:
                    file_history_map[sid] = snaps

    # 4. Scan ~/.claude/projects/**/*.jsonl, memory/, and ~/.claude/transcripts/ses_*.jsonl
    cli_projects_dir = os.path.join(claude_cli_dir, "projects")
    jsonl_by_uuid: Dict[str, Dict[str, Any]] = {}
    jsonl_by_encoded_dir: Dict[str, List[Dict[str, Any]]] = {}
    subagents_by_parent_uuid: Dict[str, List[str]] = {}
    total_subagent_bytes = 0
    total_subagent_count = 0
    memories_by_encoded_dir: Dict[str, List[Dict[str, Any]]] = {}
    global_backup_map: Dict[str, Dict[str, Any]] = {}

    if os.path.isdir(cli_projects_dir):
        for enc_folder in os.listdir(cli_projects_dir):
            enc_path = os.path.join(cli_projects_dir, enc_folder)
            if not os.path.isdir(enc_path):
                continue

            mem_dir = os.path.join(enc_path, "memory")
            if os.path.isdir(mem_dir):
                mem_files = []
                for mf in sorted(os.listdir(mem_dir)):
                    mfp = os.path.join(mem_dir, mf)
                    if os.path.isfile(mfp):
                        try:
                            with open(mfp, "r", encoding="utf-8", errors="replace") as mfile:
                                mcontent = mfile.read(50000)
                            mem_files.append({
                                "name": mf,
                                "path": mfp,
                                "size": os.path.getsize(mfp),
                                "content": mcontent,
                            })
                        except Exception:
                            pass
                if mem_files:
                    memories_by_encoded_dir[enc_folder] = mem_files

            for root, dirs, files in os.walk(enc_path):
                for fn in files:
                    if not fn.endswith(".jsonl"):
                        continue
                    fp = os.path.join(root, fn)
                    rel_to_enc = os.path.relpath(fp, enc_path)
                    if "subagents" in rel_to_enc.split(os.sep):
                        parent_sid = rel_to_enc.split(os.sep)[0]
                        subagents_by_parent_uuid.setdefault(parent_sid, []).append(fp)
                        total_subagent_count += 1
                        try:
                            total_subagent_bytes += os.path.getsize(fp)
                        except Exception:
                            pass
                    else:
                        meta = fast_scan_jsonl_metadata(fp)
                        sid = fn[:-6]
                        meta["sessionId"] = sid
                        meta["encodedFolder"] = enc_folder
                        jsonl_by_uuid[sid] = meta
                        jsonl_by_encoded_dir.setdefault(enc_folder, []).append(meta)
                        if meta.get("trackedFileBackups"):
                            global_backup_map.update(meta["trackedFileBackups"])

    # Also scan ~/.claude/transcripts/ses_*.jsonl (21 early transcripts from Jan-May 2026)
    early_transcripts_dir = os.path.join(claude_cli_dir, "transcripts")
    early_transcripts_count = 0
    if os.path.isdir(early_transcripts_dir):
        for fn in sorted(os.listdir(early_transcripts_dir)):
            if fn.endswith(".jsonl"):
                fp = os.path.join(early_transcripts_dir, fn)
                meta = fast_scan_jsonl_metadata(fp)
                sid = fn[:-6]
                meta["sessionId"] = sid
                meta["encodedFolder"] = "early-transcripts-2026Q1"
                meta["isEarlyTranscript"] = True
                jsonl_by_uuid[sid] = meta
                early_transcripts_count += 1

    # 5. Scan claude-code-sessions (both active and archived_account_scopes)
    ccs_roots = [
        os.path.join(claude_app_dir, "claude-code-sessions"),
        os.path.join(output_dir, "archived_account_scopes", "claude-code-sessions"),
    ]
    ccs_sessions: List[Dict[str, Any]] = []
    matched_jsonl_uuids: Set[str] = set()
    accounts_discovered: Dict[str, Dict[str, Any]] = {}
    scheduled_sessions_count = 0
    human_gui_sessions_count = 0
    human_gui_matched_count = 0
    seen_gui_sids: Set[str] = set()

    for ccs_dir in ccs_roots:
        if not os.path.isdir(ccs_dir):
            continue
        for root, dirs, files in os.walk(ccs_dir):
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, ccs_dir)
                parts = rel.split(os.sep)
                acc_uuid = parts[0] if len(parts) >= 2 else "unknown"
                org_uuid = parts[1] if len(parts) >= 2 else "unknown"
                acc_key = f"{acc_uuid}/{org_uuid}"

                try:
                    with open(fp, "r", encoding="utf-8") as jf:
                        data = json.load(jf)
                except Exception:
                    continue

                gui_sid = data.get("sessionId") or fn[:-5]
                if gui_sid in seen_gui_sids:
                    continue
                seen_gui_sids.add(gui_sid)

                acc_entry = accounts_discovered.setdefault(
                    acc_key,
                    {
                        "accountUuid": acc_uuid,
                        "orgUuid": org_uuid,
                        "codeSessionCount": 0,
                        "coworkSessionCount": 0,
                        "spacesCount": 0,
                    },
                )
                acc_entry["codeSessionCount"] += 1
                cli_sid = data.get("cliSessionId")
                prior_cli_sids = [
                    x for x in (data.get("priorCliSessionIds") or []) if isinstance(x, str)
                ]
                cwd = data.get("cwd")
                origin_cwd = data.get("originCwd")
                title = data.get("title")
                sched_task_id = data.get("scheduledTaskId")
                is_scheduled = bool(sched_task_id)

                if is_scheduled:
                    scheduled_sessions_count += 1
                else:
                    human_gui_sessions_count += 1

                # Match with .jsonl transcript (1. cliSessionId, 2. priorCliSessionIds, 3. cwd + customTitle)
                matched_meta = None
                if cli_sid and cli_sid in jsonl_by_uuid:
                    matched_meta = jsonl_by_uuid[cli_sid]
                    matched_jsonl_uuids.add(cli_sid)
                if not matched_meta and prior_cli_sids:
                    for psid in prior_cli_sids:
                        if psid in jsonl_by_uuid:
                            matched_meta = jsonl_by_uuid[psid]
                            matched_jsonl_uuids.add(psid)
                            cli_sid = cli_sid or psid
                            break
                if not matched_meta and cwd:
                    enc = encode_cwd_for_claude_projects(cwd)
                    candidates = jsonl_by_encoded_dir.get(enc, [])
                    for cand in candidates:
                        if title and (cand.get("customTitle") == title or cand.get("agentName") == title):
                            matched_meta = cand
                            matched_jsonl_uuids.add(cand["sessionId"])
                            cli_sid = cand["sessionId"]
                            break
                    if not matched_meta and len(candidates) == 1:
                        matched_meta = candidates[0]
                        matched_jsonl_uuids.add(matched_meta["sessionId"])
                        cli_sid = matched_meta["sessionId"]

                if matched_meta is not None and not is_scheduled:
                    human_gui_matched_count += 1

                # Also collect all subagents & file-history across both cli_sid and prior_cli_sids
                all_linked_cli_ids = [x for x in ([cli_sid] if cli_sid else []) + prior_cli_sids if x]
                sub_paths: List[str] = []
                fh_snaps: List[str] = []
                for lcid in all_linked_cli_ids:
                    for sp in subagents_by_parent_uuid.get(lcid, []):
                        if sp not in sub_paths:
                            sub_paths.append(sp)
                    for fhs in file_history_map.get(lcid, []):
                        if fhs not in fh_snaps:
                            fh_snaps.append(fhs)

                base_proj = normalize_base_project_path(
                    cwd or (matched_meta.get("cwd") if matched_meta else None),
                    origin_cwd,
                )
                wt_name = data.get("worktreeName") or extract_worktree_name(
                    cwd or (matched_meta.get("cwd") if matched_meta else None)
                )

                created_ms = data.get("createdAt")
                updated_ms = data.get("lastActivityAt") or data.get("lastFocusedAt") or created_ms

                session_record = {
                    "id": gui_sid,
                    "sessionId": gui_sid,
                    "cliSessionId": cli_sid,
                    "priorCliSessionIds": prior_cli_sids,
                    "title": title or (matched_meta.get("customTitle") if matched_meta else None) or gui_sid,
                    "cwd": cwd or (matched_meta.get("cwd") if matched_meta else None),
                    "originCwd": origin_cwd,
                    "projectPath": base_proj,
                    "worktreeName": wt_name,
                    "gitBranch": data.get("branch") or (matched_meta.get("gitBranch") if matched_meta else None),
                    "model": data.get("model") or (matched_meta.get("model") if matched_meta else None),
                    "effort": data.get("effort"),
                    "permissionMode": data.get("permissionMode"),
                    "isArchived": bool(data.get("isArchived")),
                    "isScheduled": is_scheduled,
                    "scheduledTaskId": sched_task_id,
                    "postTurnSummary": data.get("postTurnSummary"),
                    "publishedArtifacts": data.get("publishedArtifacts") or [],
                    "forkedFromSessionId": data.get("forkedFromSessionId"),
                    "accountUuid": acc_uuid,
                    "orgUuid": org_uuid,
                    "createdAt": created_ms,
                    "updatedAt": updated_ms,
                    "updatedFormatted": format_timestamp(
                        updated_ms or (matched_meta.get("lastTimestamp") if matched_meta else None)
                    ),
                    "pinnedGroup": pinned_session_map.get(gui_sid),
                    "hasTranscript": matched_meta is not None,
                    "transcriptPath": matched_meta.get("filepath") if matched_meta else None,
                    "transcriptSize": matched_meta.get("fileSize", 0) if matched_meta else 0,
                    "userTurnCount": matched_meta.get("userTurnCount", 0) if matched_meta else 0,
                    "assistantTurnCount": matched_meta.get("assistantTurnCount", 0) if matched_meta else 0,
                    "toolWriteCount": matched_meta.get("toolWriteCount", 0) if matched_meta else 0,
                    "toolEditCount": matched_meta.get("toolEditCount", 0) if matched_meta else 0,
                    "firstUserPrompt": matched_meta.get("firstUserPrompt") if matched_meta else None,
                    "lastPrompt": matched_meta.get("lastPrompt") if matched_meta else None,
                    "frameLinks": matched_meta.get("frameLinks", []) if matched_meta else [],
                    "prLinks": (matched_meta.get("prLinks", []) if matched_meta else [])
                    or ([{"prNumber": data.get("prNumber"), "prUrl": data.get("prUrl"), "prRepository": data.get("prRepository")}] if data.get("prUrl") else []),
                    "subagentPaths": sub_paths,
                    "fileHistorySnapshots": fh_snaps,
                    "guiMetadataPath": fp,
                    "sourceType": "code_gui",
                }
                ccs_sessions.append(session_record)

    # 6. Include all standalone ~/.claude/projects/*.jsonl and ~/.claude/transcripts/ses_*.jsonl
    for sid, meta in jsonl_by_uuid.items():
        if sid in matched_jsonl_uuids:
            continue
        cwd = meta.get("cwd")
        base_proj = normalize_base_project_path(cwd)
        if base_proj == "Unknown-Project":
            if meta.get("isEarlyTranscript"):
                base_proj = "Early-CLI-Transcripts (2026年1-5月)"
            elif meta.get("encodedFolder"):
                base_proj = f"[Encoded] {meta['encodedFolder']}"
        wt_name = extract_worktree_name(cwd)
        title = (
            meta.get("customTitle")
            or meta.get("agentName")
            or (meta.get("firstUserPrompt") or "")[:80]
            or f"CLI Session {sid[:12]}"
        )
        ccs_sessions.append({
            "id": sid,
            "sessionId": sid,
            "cliSessionId": sid,
            "priorCliSessionIds": [],
            "title": title,
            "cwd": cwd,
            "originCwd": None,
            "projectPath": base_proj,
            "worktreeName": wt_name,
            "gitBranch": meta.get("gitBranch"),
            "model": meta.get("model"),
            "effort": None,
            "permissionMode": None,
            "isArchived": False,
            "isScheduled": False,
            "scheduledTaskId": None,
            "postTurnSummary": None,
            "publishedArtifacts": [],
            "forkedFromSessionId": None,
            "accountUuid": meta.get("ownerAccountUuid") or "cli_local",
            "orgUuid": meta.get("ownerOrganizationUuid") or "cli_local",
            "createdAt": meta.get("firstTimestamp"),
            "updatedAt": meta.get("lastTimestamp"),
            "updatedFormatted": format_timestamp(meta.get("lastTimestamp") or meta.get("firstTimestamp")),
            "pinnedGroup": pinned_session_map.get(sid),
            "hasTranscript": True,
            "transcriptPath": meta.get("filepath"),
            "transcriptSize": meta.get("fileSize", 0),
            "userTurnCount": meta.get("userTurnCount", 0),
            "assistantTurnCount": meta.get("assistantTurnCount", 0),
            "toolWriteCount": meta.get("toolWriteCount", 0),
            "toolEditCount": meta.get("toolEditCount", 0),
            "firstUserPrompt": meta.get("firstUserPrompt"),
            "lastPrompt": meta.get("lastPrompt"),
            "frameLinks": meta.get("frameLinks", []),
            "prLinks": meta.get("prLinks", []),
            "subagentPaths": subagents_by_parent_uuid.get(sid, []),
            "fileHistorySnapshots": file_history_map.get(sid, []),
            "guiMetadataPath": None,
            "sourceType": "early_transcript" if meta.get("isEarlyTranscript") else "claude_code_jsonl",
        })

    # 7. Group Code sessions into Project Folders
    projects_by_path: Dict[str, Dict[str, Any]] = {}
    sidebar_order = {p: idx for idx, p in enumerate(gui_state.get("sidebarProjectGroups", []))}

    for sess in ccs_sessions:
        ppath = sess["projectPath"]
        proj = projects_by_path.get(ppath)
        if not proj:
            enc = encode_cwd_for_claude_projects(ppath)
            exists_on_disk = os.path.isdir(ppath) if os.path.isabs(ppath) else False
            proj = {
                "projectPath": ppath,
                "projectName": os.path.basename(ppath.rstrip("/")) if ppath not in ("/", "") else ppath,
                "existsOnDisk": exists_on_disk,
                "sidebarOrder": sidebar_order.get(ppath, 9999),
                "inGuiSidebar": ppath in sidebar_order,
                "sessions": [],
                "sessionCount": 0,
                "humanSessionCount": 0,
                "scheduledSessionCount": 0,
                "transcriptCount": 0,
                "totalTranscriptBytes": 0,
                "worktrees": [],
                "memories": memories_by_encoded_dir.get(enc, []),
                "prLinks": [],
                "frameLinks": [],
                "lastUpdatedFormatted": "",
            }
            projects_by_path[ppath] = proj

        proj["sessions"].append(sess)
        proj["sessionCount"] += 1
        if sess.get("isScheduled"):
            proj["scheduledSessionCount"] += 1
        else:
            proj["humanSessionCount"] += 1
        if sess["hasTranscript"]:
            proj["transcriptCount"] += 1
            proj["totalTranscriptBytes"] += sess.get("transcriptSize", 0)
        if sess.get("worktreeName") and sess["worktreeName"] not in proj["worktrees"]:
            proj["worktrees"].append(sess["worktreeName"])
        for pr in sess.get("prLinks", []):
            if pr not in proj["prLinks"]:
                proj["prLinks"].append(pr)
        for fl in sess.get("frameLinks", []):
            if fl not in proj["frameLinks"]:
                proj["frameLinks"].append(fl)
        if sess["updatedFormatted"] > proj["lastUpdatedFormatted"]:
            proj["lastUpdatedFormatted"] = sess["updatedFormatted"]

    for proj in projects_by_path.values():
        proj["sessions"].sort(
            key=lambda s: (1 if s["hasTranscript"] else 0, s.get("updatedFormatted") or ""),
            reverse=True,
        )

    sorted_projects = sorted(
        projects_by_path.values(),
        key=lambda p: (
            1 if p["transcriptCount"] > 0 else 0,
            1 if p["inGuiSidebar"] else 0,
            p["lastUpdatedFormatted"],
        ),
        reverse=True,
    )

    # 8. Scan local-agent-mode-sessions (Cowork Spaces & Sessions, both active and archived)
    lam_roots = [
        os.path.join(claude_app_dir, "local-agent-mode-sessions"),
        os.path.join(output_dir, "archived_account_scopes", "local-agent-mode-sessions"),
    ]
    cowork_spaces: List[Dict[str, Any]] = []
    cowork_sessions: List[Dict[str, Any]] = []
    cowork_jsonl_count = 0
    seen_space_ids: Set[str] = set()
    seen_cowork_sids: Set[str] = set()

    for lam_dir in lam_roots:
        if not os.path.isdir(lam_dir):
            continue
        for root, dirs, files in os.walk(lam_dir):
            if "spaces.json" in files:
                sp_path = os.path.join(root, "spaces.json")
                rel = os.path.relpath(root, lam_dir)
                parts = rel.split(os.sep)
                acc_uuid = parts[0] if len(parts) >= 2 else "unknown"
                org_uuid = parts[1] if len(parts) >= 2 else "unknown"
                acc_key = f"{acc_uuid}/{org_uuid}"
                try:
                    with open(sp_path, "r", encoding="utf-8") as spf:
                        sp_data = json.load(spf)
                    for s in sp_data.get("spaces", []):
                        sp_id = s.get("id")
                        if sp_id in seen_space_ids:
                            continue
                        seen_space_ids.add(sp_id)
                        folders = [f.get("path") for f in s.get("folders", []) if isinstance(f, dict) and f.get("path")]
                        cowork_spaces.append({
                            "id": sp_id,
                            "name": s.get("name"),
                            "folders": folders,
                            "foldersExist": [os.path.exists(fp) for fp in folders],
                            "instructions": s.get("instructions", ""),
                            "accountUuid": acc_uuid,
                            "orgUuid": org_uuid,
                            "createdAt": s.get("createdAt"),
                            "updatedAt": s.get("updatedAt"),
                            "updatedFormatted": format_timestamp(s.get("updatedAt") or s.get("createdAt")),
                        })
                        if acc_key in accounts_discovered:
                            accounts_discovered[acc_key]["spacesCount"] += 1
                except Exception:
                    pass

            for fn in files:
                if fn.startswith("local_") and fn.endswith(".json"):
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(root, lam_dir)
                    parts = rel.split(os.sep)
                    acc_uuid = parts[0] if len(parts) >= 2 else "unknown"
                    org_uuid = parts[1] if len(parts) >= 2 else "unknown"
                    acc_key = f"{acc_uuid}/{org_uuid}"

                    try:
                        with open(fp, "r", encoding="utf-8") as cf_in:
                            data = json.load(cf_in)
                    except Exception:
                        continue

                    sid = data.get("sessionId") or fn[:-5]
                    if sid in seen_cowork_sids:
                        continue
                    seen_cowork_sids.add(sid)

                    accounts_discovered.setdefault(
                        acc_key,
                        {
                            "accountUuid": acc_uuid,
                            "orgUuid": org_uuid,
                            "codeSessionCount": 0,
                            "coworkSessionCount": 0,
                            "spacesCount": 0,
                        },
                    )["coworkSessionCount"] += 1
                    sdir = os.path.join(root, fn[:-5])
                    audit_path = os.path.join(sdir, "audit.jsonl")
                    inner_jsonls = glob.glob(os.path.join(sdir, ".claude", "projects", "**", "*.jsonl"), recursive=True)
                    cowork_jsonl_count += len(inner_jsonls) + (1 if os.path.exists(audit_path) else 0)
                    primary_inner = [p for p in inner_jsonls if "subagents" not in p]
                    sub_inner = [p for p in inner_jsonls if "subagents" in p]
                    transcript_path = primary_inner[0] if primary_inner else (audit_path if os.path.exists(audit_path) else None)
                    t_meta = fast_scan_jsonl_metadata(transcript_path) if transcript_path else {}

                    outputs_dir = os.path.join(sdir, "outputs")
                    uploads_dir = os.path.join(sdir, "uploads")
                    output_files = []
                    if os.path.isdir(outputs_dir):
                        for r2, _, f2 in os.walk(outputs_dir):
                            for ofn in f2:
                                if not ofn.startswith("."):
                                    ofp = os.path.join(r2, ofn)
                                    output_files.append({
                                        "name": os.path.relpath(ofp, outputs_dir),
                                        "path": ofp,
                                        "size": os.path.getsize(ofp),
                                    })
                    upload_files = []
                    if os.path.isdir(uploads_dir):
                        for r2, _, f2 in os.walk(uploads_dir):
                            for ufn in f2:
                                if not ufn.startswith("."):
                                    ufp = os.path.join(r2, ufn)
                                    upload_files.append({
                                        "name": os.path.relpath(ufp, uploads_dir),
                                        "path": ufp,
                                        "size": os.path.getsize(ufp),
                                    })

                    ufolders = data.get("userSelectedFolders") or []
                    created_ms = data.get("createdAt")
                    updated_ms = data.get("lastActivityAt") or created_ms

                    cowork_sessions.append({
                        "id": sid,
                        "sessionId": sid,
                        "cliSessionId": data.get("cliSessionId"),
                        "title": data.get("title") or t_meta.get("customTitle") or sid,
                        "initialMessage": (data.get("initialMessage") or "")[:300],
                        "userSelectedFolders": ufolders,
                        "projectPath": ufolders[0] if ufolders else "Cowork-General",
                        "model": data.get("model") or t_meta.get("model"),
                        "accountUuid": acc_uuid,
                        "orgUuid": org_uuid,
                        "createdAt": created_ms,
                        "updatedAt": updated_ms,
                        "updatedFormatted": format_timestamp(updated_ms or t_meta.get("lastTimestamp")),
                        "hasTranscript": transcript_path is not None,
                        "transcriptPath": transcript_path,
                        "auditPath": audit_path if os.path.exists(audit_path) else None,
                        "transcriptSize": t_meta.get("fileSize", 0),
                        "userTurnCount": t_meta.get("userTurnCount", 0),
                        "assistantTurnCount": t_meta.get("assistantTurnCount", 0),
                        "toolWriteCount": t_meta.get("toolWriteCount", 0),
                        "subagentPaths": sub_inner,
                        "outputFiles": output_files,
                        "uploadFiles": upload_files,
                        "guiMetadataPath": fp,
                        "sourceType": "cowork_gui",
                    })

    cowork_sessions.sort(key=lambda x: x.get("updatedFormatted") or "", reverse=True)

    # 9. Scan Chromium Simple Cache for Cloud Conversations, Projects & Sandbox Artifacts
    out_cloud_dir = os.path.join(output_dir, "cloud_chats_and_projects")
    cloud_data = scan_all_chromium_caches(claude_app_dir, out_cloud_dir)
    prev_cloud = prev_catalog.get("cloudData") or {}
    if prev_cloud:
        seen_cids = {c["sessionId"] for c in cloud_data["conversations"]}
        for pc in prev_cloud.get("conversations", []):
            if pc.get("sessionId") not in seen_cids:
                cloud_data["conversations"].append(pc)
        seen_vshas = {v["sha256"] for v in cloud_data.get("conversationResponseVersions", []) if "sha256" in v}
        for pv in prev_cloud.get("conversationResponseVersions", []):
            if pv.get("sha256") not in seen_vshas:
                cloud_data.setdefault("conversationResponseVersions", []).append(pv)
        seen_puids = {p["uuid"] for p in cloud_data["projects"]}
        for pp in prev_cloud.get("projects", []):
            if pp.get("uuid") not in seen_puids:
                cloud_data["projects"].append(pp)
        seen_sfids = {s["id"] for s in cloud_data["sandboxFiles"]}
        for ps in prev_cloud.get("sandboxFiles", []):
            if ps.get("id") not in seen_sfids:
                cloud_data["sandboxFiles"].append(ps)

    # 10. Scan Auxiliary Evidence (file-history with resolved names, handoff-history, git-shadow, etc.)
    aux_evidence = scan_auxiliary_evidence(claude_cli_dir, claude_app_dir, global_backup_map)

    # 11. Export Cowork outputs & reconstruct missing folder projects into recovered_data/
    cowork_export_dir = os.path.join(output_dir, "cowork_spaces")
    os.makedirs(cowork_export_dir, exist_ok=True)
    reconstructed_missing_folders: List[Dict[str, Any]] = []

    for cs in cowork_sessions:
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", cs["title"] or "cowork")[:50].strip("_")
        s_export_dir = os.path.join(cowork_export_dir, f"{cs['updatedFormatted'][:10]}_{safe_title}_{cs['id'][:12]}")
        os.makedirs(s_export_dir, exist_ok=True)
        if cs["transcriptPath"] and os.path.exists(cs["transcriptPath"]):
            try:
                parsed = parse_jsonl_full(cs["transcriptPath"])
                md = render_conversation_to_markdown(parsed, cs)
                md_out = os.path.join(s_export_dir, "conversation.md")
                with open(md_out, "w", encoding="utf-8") as mf:
                    mf.write(md)
                cs["exportedMdPath"] = md_out

                for uf in cs.get("userSelectedFolders", []):
                    if uf and not os.path.exists(uf):
                        recon_dir = os.path.join(output_dir, "reconstructed_missing_folders", os.path.basename(uf))
                        os.makedirs(recon_dir, exist_ok=True)
                        copied_outputs = []
                        for of in cs.get("outputFiles", []):
                            dst = os.path.join(recon_dir, of["name"])
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            if os.path.exists(of["path"]):
                                shutil.copy2(of["path"], dst)
                                copied_outputs.append(of["name"])
                        for rf in parsed.get("reconstructedFiles", []):
                            fname = rf.get("fileName") or "file.txt"
                            dst = os.path.join(recon_dir, "from_tool_calls", fname)
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            with open(dst, "w", encoding="utf-8") as wf:
                                wf.write(rf.get("content", ""))
                        reconstructed_missing_folders.append({
                            "originalPath": uf,
                            "reconstructedPath": recon_dir,
                            "sessionId": cs["id"],
                            "sessionTitle": cs["title"],
                            "restoredOutputFilesCount": len(copied_outputs),
                            "restoredToolFilesCount": len(parsed.get("reconstructedFiles", [])),
                        })
            except Exception:
                pass

    elapsed = round(time.time() - t0, 2)

    main_bytes = sum(s.get("transcriptSize", 0) for s in ccs_sessions)
    total_main_transcripts = sum(1 for s in ccs_sessions if s["hasTranscript"])
    total_all_jsonls = total_main_transcripts + total_subagent_count + cowork_jsonl_count + (1 if aux_evidence["cliInputHistoryPath"] else 0)

    catalog = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsedSeconds": elapsed,
        "summary": {
            "accountsDiscovered": list(accounts_discovered.values()),
            "totalCodeProjects": len(sorted_projects),
            "totalCodeSessions": len(ccs_sessions),
            "totalGuiCodeSessions": scheduled_sessions_count + human_gui_sessions_count,
            "totalHumanGuiCodeSessions": human_gui_sessions_count,
            "totalHumanGuiMatchedSessions": human_gui_matched_count,
            "totalScheduledGuiCodeSessions": scheduled_sessions_count,
            "totalEarlyTranscripts": early_transcripts_count,
            "totalCodeTranscripts": total_main_transcripts,
            "totalSubagentTranscripts": total_subagent_count,
            "totalCoworkJsonlFiles": cowork_jsonl_count,
            "totalAllJsonlFiles": total_all_jsonls,
            "totalCodeTranscriptSizeMB": round(main_bytes / (1024 * 1024), 2),
            "totalSubagentSizeMB": round(total_subagent_bytes / (1024 * 1024), 2),
            "totalAllTranscriptSizeMB": round((main_bytes + total_subagent_bytes) / (1024 * 1024), 2),
            "totalCoworkSpaces": len(cowork_spaces),
            "totalCoworkSessions": len(cowork_sessions),
            "totalCoworkOutputFiles": sum(len(s.get("outputFiles", [])) for s in cowork_sessions),
            "totalCloudConversations": len(cloud_data["conversations"]),
            "totalCloudConversationVersions": len(cloud_data["conversationResponseVersions"]),
            "totalCloudProjects": len(cloud_data["projects"]),
            "totalCloudSandboxFiles": len(cloud_data["sandboxFiles"]),
            "totalWorktrees": len(worktrees_map),
            "totalFileHistorySessions": len(file_history_map),
            "totalFileHistorySnapshots": len(aux_evidence["fileHistorySnapshots"]),
            "totalMappedFileHistorySnapshots": sum(1 for x in aux_evidence["fileHistorySnapshots"] if x["isMappedToOriginalPath"]),
            "totalShadowGitRepos": len(aux_evidence["shadowGitRepos"]),
            "totalHandoffFiles": len(aux_evidence["handoffFiles"]),
            "totalPendingUploads": len(aux_evidence["pendingUploads"]),
            "reconstructedMissingFolders": reconstructed_missing_folders,
        },
        "guiState": gui_state,
        "pinnedGroups": pinned_groups_list,
        "codeProjects": sorted_projects,
        "coworkSpaces": cowork_spaces,
        "coworkSessions": cowork_sessions,
        "cloudData": cloud_data,
        "auxiliaryEvidence": aux_evidence,
        "worktrees": list(worktrees_map.values()),
    }

    catalog_path = os.path.join(output_dir, "catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as cf:
        json.dump(catalog, cf, ensure_ascii=False, indent=2)

    return catalog
