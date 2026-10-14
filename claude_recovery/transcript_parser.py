#!/usr/bin/env python3
"""
Parser and code/artifact reconstructor for Claude transcripts:
1. ~/.claude/projects/**/*.jsonl (Claude Code CLI & Claude Desktop "Code" GUI tab)
2. ~/Library/Application Support/Claude/local-agent-mode-sessions/**/audit.jsonl (Cowork mode)
3. ~/.claude/transcripts/ses_*.jsonl (Early Jan-May 2026 flat JSONL transcripts)
4. Cloud chat_conversations JSON objects recovered from Chromium Simple Cache
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def format_timestamp(ts: Any) -> str:
    """Normalize epoch ms, epoch sec, or ISO string to YYYY-MM-DD HH:MM:SS."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        if ts > 1e11:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)
    if isinstance(ts, str):
        try:
            s = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ts[:19].replace("T", " ")
    return str(ts)


def normalize_base_project_path(cwd: Optional[str], origin_cwd: Optional[str] = None) -> str:
    """
    Given a cwd (which might be inside /.claude/worktrees/<name> or scratchpad),
    return the canonical base project folder path.
    """
    if origin_cwd and os.path.isabs(origin_cwd):
        return origin_cwd.rstrip("/")
    if not cwd:
        return "Unknown-Project"
    p = cwd.rstrip("/")
    if "/.claude/worktrees/" in p:
        base = p.split("/.claude/worktrees/")[0]
        if base:
            return base
    if p.startswith("/private/tmp/claude-") and "--Users-" in p:
        return p
    return p


def extract_worktree_name(cwd: Optional[str]) -> Optional[str]:
    if cwd and "/.claude/worktrees/" in cwd:
        rest = cwd.split("/.claude/worktrees/", 1)[1]
        return rest.split("/")[0]
    return None


def fast_scan_jsonl_metadata(filepath: str) -> Dict[str, Any]:
    """
    Fast metadata scan of a .jsonl transcript file without full DOM construction.
    Supports both standard ~/.claude/projects/*.jsonl and ~/.claude/transcripts/ses_*.jsonl.
    """
    info: Dict[str, Any] = {
        "filepath": filepath,
        "fileSize": 0,
        "sessionId": os.path.basename(filepath)[:-6] if filepath.endswith(".jsonl") else os.path.basename(filepath),
        "cwd": None,
        "gitBranch": None,
        "version": None,
        "entrypoint": None,
        "customTitle": None,
        "agentName": None,
        "lastPrompt": None,
        "firstUserPrompt": None,
        "ownerAccountUuid": None,
        "ownerOrganizationUuid": None,
        "bridgeSessionId": None,
        "frameLinks": [],
        "prLinks": [],
        "trackedFileBackups": {},
        "userTurnCount": 0,
        "assistantTurnCount": 0,
        "toolWriteCount": 0,
        "toolEditCount": 0,
        "firstTimestamp": None,
        "lastTimestamp": None,
        "model": None,
    }
    if not os.path.exists(filepath):
        return info

    try:
        st = os.stat(filepath)
        info["fileSize"] = st.st_size
        mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        mtime_iso = None

    special_markers = (
        '"custom-title"',
        '"agent-name"',
        '"last-prompt"',
        '"bridge-session"',
        '"frame-link"',
        '"pr-link"',
        '"trackedFileBackups"',
    )

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                if '"type":"user"' in line or '"type": "user"' in line:
                    if info["firstUserPrompt"] is None or idx < 50 or info["cwd"] is None:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "user":
                                if not info["cwd"] and obj.get("cwd"):
                                    info["cwd"] = obj.get("cwd")
                                if not info["gitBranch"] and obj.get("gitBranch"):
                                    info["gitBranch"] = obj.get("gitBranch")
                                if not info["version"] and obj.get("version"):
                                    info["version"] = obj.get("version")
                                if not info["entrypoint"] and obj.get("entrypoint"):
                                    info["entrypoint"] = obj.get("entrypoint")
                                ts = obj.get("timestamp") or obj.get("_audit_timestamp")
                                if ts:
                                    if not info["firstTimestamp"]:
                                        info["firstTimestamp"] = ts
                                    info["lastTimestamp"] = ts
                                text = _extract_user_prompt_text(obj.get("message") or obj.get("content"))
                                if text:
                                    info["userTurnCount"] += 1
                                    if not info["firstUserPrompt"]:
                                        info["firstUserPrompt"] = text[:300]
                        except Exception:
                            pass
                    else:
                        if '"tool_result"' not in line:
                            info["userTurnCount"] += 1

                elif '"type":"assistant"' in line or '"type": "assistant"' in line:
                    info["assistantTurnCount"] += 1
                    if info["model"] is None or not info["lastTimestamp"]:
                        try:
                            obj = json.loads(line)
                            msg = obj.get("message", {})
                            if isinstance(msg, dict) and msg.get("model"):
                                info["model"] = msg.get("model")
                            ts = obj.get("timestamp") or obj.get("_audit_timestamp")
                            if ts:
                                if not info["firstTimestamp"]:
                                    info["firstTimestamp"] = ts
                                info["lastTimestamp"] = ts
                        except Exception:
                            pass
                    if '"Write"' in line or '"write_file"' in line or '"create_file"' in line:
                        info["toolWriteCount"] += 1
                    if '"Edit"' in line or '"MultiEdit"' in line or '"str_replace"' in line:
                        info["toolEditCount"] += 1

                elif ('"type":"tool_use"' in line or '"type": "tool_use"' in line) and not info["cwd"]:
                    # In ~/.claude/transcripts/ses_*.jsonl, tool_use is top-level and often has path in tool_input
                    try:
                        obj = json.loads(line)
                        tinp = obj.get("tool_input") or {}
                        if isinstance(tinp, dict):
                            cand_path = tinp.get("path") or tinp.get("file_path") or tinp.get("cwd")
                            if isinstance(cand_path, str) and os.path.isabs(cand_path) and not cand_path.startswith("/dev/"):
                                info["cwd"] = cand_path if os.path.isdir(cand_path) else os.path.dirname(cand_path)
                    except Exception:
                        pass

                elif idx < 25 and (info["cwd"] is None or info["firstTimestamp"] is None):
                    try:
                        obj = json.loads(line)
                        if not info["cwd"] and obj.get("cwd"):
                            info["cwd"] = obj.get("cwd")
                        if not info["gitBranch"] and obj.get("gitBranch"):
                            info["gitBranch"] = obj.get("gitBranch")
                        if not info["entrypoint"] and obj.get("entrypoint"):
                            info["entrypoint"] = obj.get("entrypoint")
                        if not info["model"] and obj.get("model"):
                            info["model"] = obj.get("model")
                        ts = obj.get("timestamp") or obj.get("_audit_timestamp")
                        if ts and not info["firstTimestamp"]:
                            info["firstTimestamp"] = ts
                    except Exception:
                        pass

                if any(m in line for m in special_markers):
                    try:
                        obj = json.loads(line)
                        t = obj.get("type")
                        if t == "custom-title" and obj.get("customTitle"):
                            info["customTitle"] = obj.get("customTitle")
                        elif t == "agent-name" and obj.get("agentName"):
                            info["agentName"] = obj.get("agentName")
                        elif t == "last-prompt" and obj.get("lastPrompt"):
                            info["lastPrompt"] = obj.get("lastPrompt")[:300]
                        elif t == "bridge-session":
                            info["ownerAccountUuid"] = obj.get("ownerAccountUuid") or info["ownerAccountUuid"]
                            info["ownerOrganizationUuid"] = obj.get("ownerOrganizationUuid") or info["ownerOrganizationUuid"]
                            info["bridgeSessionId"] = obj.get("bridgeSessionId") or info["bridgeSessionId"]
                        elif t == "frame-link":
                            info["frameLinks"].append({
                                "title": obj.get("title"),
                                "path": obj.get("path"),
                                "frameUrl": obj.get("frameUrl"),
                                "timestamp": obj.get("timestamp"),
                            })
                        elif t == "pr-link":
                            info["prLinks"].append({
                                "prNumber": obj.get("prNumber"),
                                "prUrl": obj.get("prUrl"),
                                "prRepository": obj.get("prRepository"),
                                "timestamp": obj.get("timestamp"),
                            })
                        elif t == "file-history-snapshot":
                            snap = obj.get("snapshot") or {}
                            backups = snap.get("trackedFileBackups") or {}
                            if isinstance(backups, dict):
                                for orig_rel, binfo in backups.items():
                                    if isinstance(binfo, dict) and binfo.get("backupFileName"):
                                        bfn = binfo["backupFileName"]
                                        rparent = binfo.get("realParentDir") or ""
                                        full_orig = orig_rel if os.path.isabs(orig_rel) else os.path.join(rparent, os.path.basename(orig_rel))
                                        info["trackedFileBackups"][bfn] = {
                                            "backupFileName": bfn,
                                            "originalPath": full_orig,
                                            "originalRelPath": orig_rel,
                                            "realParentDir": rparent,
                                            "version": binfo.get("version"),
                                            "backupTime": binfo.get("backupTime"),
                                        }
                    except Exception:
                        pass
    except Exception:
        pass

    if not info["lastTimestamp"] and mtime_iso:
        info["lastTimestamp"] = mtime_iso
    if not info["firstTimestamp"] and mtime_iso:
        info["firstTimestamp"] = mtime_iso

    return info


def _extract_user_prompt_text(message: Any) -> str:
    """Extract clean human text from a user message payload (skipping pure tool_result blocks)."""
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                btype = item.get("type")
                if btype == "text" and item.get("text"):
                    parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def parse_jsonl_full(filepath: str) -> Dict[str, Any]:
    """
    Full 1:1 parser for a .jsonl, audit.jsonl, or early ses_*.jsonl transcript.
    Returns structured conversation turns, attachments, metadata, and reconstructed files.
    """
    meta = fast_scan_jsonl_metadata(filepath)
    turns: List[Dict[str, Any]] = []
    attachments: List[Dict[str, Any]] = []
    reconstructed_files: Dict[str, Dict[str, Any]] = {}
    tool_results_map: Dict[str, Any] = {}

    if not os.path.exists(filepath):
        return {"meta": meta, "turns": [], "attachments": [], "reconstructedFiles": []}

    raw_records: List[Dict[str, Any]] = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    raw_records.append(obj)
            except Exception:
                continue

    # First pass: collect tool_result blocks so we can pair them with tool_use blocks
    for obj in raw_records:
        if obj.get("type") == "user":
            msg = obj.get("message", {})
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for blk in msg["content"]:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tuid = blk.get("tool_use_id")
                        if tuid:
                            tool_results_map[tuid] = {
                                "is_error": bool(blk.get("is_error")),
                                "content": _stringify_tool_result_content(blk.get("content")),
                            }

    # Second pass: build turns & reconstruct written/edited files
    for obj in raw_records:
        rtype = obj.get("type")
        ts = obj.get("timestamp") or obj.get("_audit_timestamp")

        if rtype == "attachment":
            att = obj.get("attachment") or {}
            if isinstance(att, dict):
                attachments.append({
                    "uuid": obj.get("uuid"),
                    "timestamp": ts,
                    "type": att.get("type") or att.get("attachment_type") or "attachment",
                    "name": att.get("file_name") or att.get("name") or att.get("path") or "attachment",
                    "content": _truncate_large(att.get("content") or att.get("rendered") or obj.get("rendered") or "", 10000),
                })

        elif rtype == "user":
            msg = obj.get("message") if "message" in obj else obj.get("content")
            blocks = []
            if isinstance(msg, str):
                blocks.append({"type": "text", "text": msg})
            elif isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, str):
                            blocks.append({"type": "text", "text": blk})
                        elif isinstance(blk, dict):
                            bt = blk.get("type")
                            if bt == "text":
                                blocks.append({"type": "text", "text": blk.get("text", "")})
                            elif bt == "image":
                                blocks.append({"type": "image", "info": "User uploaded image"})
                            elif bt == "document":
                                blocks.append({"type": "document", "title": blk.get("title") or "Document"})
            if blocks:
                turns.append({
                    "role": "user",
                    "uuid": obj.get("uuid"),
                    "timestamp": ts,
                    "timestampFormatted": format_timestamp(ts),
                    "cwd": obj.get("cwd"),
                    "gitBranch": obj.get("gitBranch"),
                    "blocks": blocks,
                })

        elif rtype == "assistant":
            msg = obj.get("message")
            blocks = []
            model = None
            msg_id = None
            usage = None
            if isinstance(msg, dict):
                model = msg.get("model")
                msg_id = msg.get("id")
                usage = msg.get("usage")
                content = msg.get("content")
                if isinstance(content, str):
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt == "text":
                            blocks.append({"type": "text", "text": blk.get("text", "")})
                        elif bt == "thinking":
                            blocks.append({
                                "type": "thinking",
                                "thinking": blk.get("thinking") or blk.get("text") or "",
                            })
                        elif bt == "tool_use":
                            tname = blk.get("name", "tool")
                            tuid = blk.get("id", "")
                            tinput = blk.get("input") or {}
                            tresult = tool_results_map.get(tuid)
                            blocks.append({
                                "type": "tool_use",
                                "id": tuid,
                                "name": tname,
                                "input": tinput,
                                "result": tresult,
                            })
                            _apply_file_reconstruction(reconstructed_files, tname, tinput, ts)
            elif isinstance(obj.get("content"), str):
                # Flat format in ~/.claude/transcripts/ses_*.jsonl
                blocks.append({"type": "text", "text": obj["content"]})

            if blocks:
                if turns and turns[-1]["role"] == "assistant" and turns[-1].get("messageId") == msg_id and msg_id:
                    turns[-1]["blocks"].extend(blocks)
                else:
                    turns.append({
                        "role": "assistant",
                        "uuid": obj.get("uuid"),
                        "messageId": msg_id,
                        "model": model,
                        "timestamp": ts,
                        "timestampFormatted": format_timestamp(ts),
                        "usage": usage,
                        "blocks": blocks,
                    })

        elif rtype == "tool_use":
            # Flat format in ~/.claude/transcripts/ses_*.jsonl
            tname = obj.get("tool_name") or obj.get("name") or "tool"
            tinput = obj.get("tool_input") or obj.get("input") or {}
            blk = {
                "type": "tool_use",
                "id": "",
                "name": tname,
                "input": tinput,
                "result": None,
            }
            _apply_file_reconstruction(reconstructed_files, tname, tinput, ts)
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["blocks"].append(blk)
            else:
                turns.append({
                    "role": "assistant",
                    "uuid": None,
                    "messageId": None,
                    "model": None,
                    "timestamp": ts,
                    "timestampFormatted": format_timestamp(ts),
                    "blocks": [blk],
                })

        elif rtype == "tool_result":
            # Flat format in ~/.claude/transcripts/ses_*.jsonl
            tname = obj.get("tool_name") or obj.get("name") or "tool"
            tout = obj.get("tool_output") if "tool_output" in obj else obj.get("content")
            blk = {
                "type": "tool_result",
                "name": tname,
                "is_error": False,
                "content": _stringify_tool_result_content(tout),
            }
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["blocks"].append(blk)

    return {
        "meta": meta,
        "turns": turns,
        "attachments": attachments[:50],
        "reconstructedFiles": list(reconstructed_files.values()),
    }


def _stringify_tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return _truncate_large(content, 20000)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
        return _truncate_large("\n".join(parts), 20000)
    return _truncate_large(json.dumps(content, ensure_ascii=False), 20000)


def _truncate_large(text: str, max_chars: int = 20000) -> str:
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [内容过长已截断，总长度 {len(text)} 字符] ..."


def _apply_file_reconstruction(
    files_map: Dict[str, Dict[str, Any]],
    tool_name: str,
    tool_input: Dict[str, Any],
    ts: Any,
) -> None:
    if not isinstance(tool_input, dict):
        return
    fpath = tool_input.get("file_path") or tool_input.get("path")
    if not fpath or not isinstance(fpath, str):
        return

    if tool_name in ("Write", "write_file", "create_file"):
        content = tool_input.get("content") or tool_input.get("file_text") or ""
        files_map[fpath] = {
            "filePath": fpath,
            "fileName": os.path.basename(fpath),
            "lastOperation": "Write",
            "timestamp": ts,
            "timestampFormatted": format_timestamp(ts),
            "content": content,
            "size": len(content.encode("utf-8", errors="replace")),
            "existsOnDisk": os.path.exists(fpath),
            "edits": files_map.get(fpath, {}).get("edits", []) + [{"op": "Write", "timestamp": ts}],
        }
    elif tool_name in ("Edit", "replace", "str_replace"):
        old_s = tool_input.get("old_string") or tool_input.get("old_str") or ""
        new_s = tool_input.get("new_string") or tool_input.get("new_str") or ""
        entry = files_map.get(fpath)
        if not entry:
            initial_content = ""
            if os.path.exists(fpath) and os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        initial_content = f.read(500_000)
                except Exception:
                    pass
            entry = {
                "filePath": fpath,
                "fileName": os.path.basename(fpath),
                "lastOperation": "Edit",
                "timestamp": ts,
                "timestampFormatted": format_timestamp(ts),
                "content": initial_content,
                "size": len(initial_content.encode("utf-8", errors="replace")),
                "existsOnDisk": os.path.exists(fpath),
                "edits": [],
            }
            files_map[fpath] = entry
        else:
            if old_s and old_s in entry["content"]:
                entry["content"] = entry["content"].replace(old_s, new_s, 1)
                entry["size"] = len(entry["content"].encode("utf-8", errors="replace"))
        entry["lastOperation"] = "Edit"
        entry["timestamp"] = ts
        entry["timestampFormatted"] = format_timestamp(ts)
        entry["edits"].append({
            "op": "Edit",
            "timestamp": ts,
            "old_string": old_s[:2000],
            "new_string": new_s[:2000],
        })
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        for ed in edits:
            if isinstance(ed, dict):
                _apply_file_reconstruction(
                    files_map,
                    "Edit",
                    {"file_path": fpath, "old_string": ed.get("old_string", ""), "new_string": ed.get("new_string", "")},
                    ts,
                )


def parse_cloud_conversation(conv_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a cloud chat_conversations JSON object (from Chromium Simple Cache)
    into the same unified multi-turn conversation format.
    """
    turns = []
    reconstructed_files: Dict[str, Dict[str, Any]] = {}
    for msg in conv_obj.get("chat_messages", []):
        sender = msg.get("sender", "human")
        role = "user" if sender == "human" else "assistant"
        ts = msg.get("created_at") or msg.get("updated_at")
        blocks = []
        content_list = msg.get("content") or []
        if isinstance(content_list, list) and content_list:
            for c in content_list:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    blocks.append({"type": "text", "text": c.get("text", "")})
                elif ct == "thinking":
                    blocks.append({"type": "thinking", "thinking": c.get("thinking") or c.get("text") or ""})
                elif ct == "tool_use":
                    tname = c.get("name", "tool")
                    tinput = c.get("input") or {}
                    blocks.append({
                        "type": "tool_use",
                        "id": c.get("id", ""),
                        "name": tname,
                        "input": tinput,
                        "result": None,
                    })
                    if tname in ("artifacts", "create_file", "str_replace"):
                        fp = tinput.get("path") or tinput.get("id") or tinput.get("title")
                        content_str = tinput.get("content") or tinput.get("file_text") or ""
                        if fp and content_str:
                            reconstructed_files[str(fp)] = {
                                "filePath": str(fp),
                                "fileName": os.path.basename(str(fp)),
                                "lastOperation": tname,
                                "timestamp": ts,
                                "timestampFormatted": format_timestamp(ts),
                                "content": content_str,
                                "size": len(content_str.encode("utf-8", errors="replace")),
                                "existsOnDisk": False,
                                "edits": [{"op": tname, "timestamp": ts}],
                            }
                elif ct == "tool_result":
                    blocks.append({
                        "type": "tool_result",
                        "name": c.get("name", ""),
                        "is_error": bool(c.get("is_error")),
                        "content": _stringify_tool_result_content(c.get("content")),
                    })
        elif msg.get("text"):
            blocks.append({"type": "text", "text": msg.get("text", "")})

        for att in msg.get("attachments", []) + msg.get("files", []) + msg.get("files_v2", []):
            if isinstance(att, dict):
                fname = att.get("file_name") or att.get("name") or "attachment"
                extracted = att.get("extracted_content") or ""
                blocks.append({
                    "type": "attachment",
                    "fileName": fname,
                    "fileType": att.get("file_type") or att.get("file_kind") or "",
                    "content": _truncate_large(extracted, 10000),
                })

        turns.append({
            "role": role,
            "uuid": msg.get("uuid"),
            "timestamp": ts,
            "timestampFormatted": format_timestamp(ts),
            "model": conv_obj.get("model"),
            "blocks": blocks,
        })

    return {
        "meta": {
            "sessionId": conv_obj.get("uuid"),
            "customTitle": conv_obj.get("name"),
            "summary": conv_obj.get("summary"),
            "model": conv_obj.get("model"),
            "projectUuid": conv_obj.get("project_uuid"),
            "firstTimestamp": conv_obj.get("created_at"),
            "lastTimestamp": conv_obj.get("updated_at"),
            "userTurnCount": sum(1 for t in turns if t["role"] == "user"),
            "assistantTurnCount": sum(1 for t in turns if t["role"] == "assistant"),
        },
        "turns": turns,
        "attachments": [],
        "reconstructedFiles": list(reconstructed_files.values()),
    }


def render_conversation_to_markdown(parsed: Dict[str, Any], header_info: Optional[Dict[str, Any]] = None) -> str:
    """Render a parsed conversation (Code, Cowork, or Cloud) into clean 1:1 Markdown."""
    meta = parsed.get("meta", {})
    info = header_info or {}
    title = info.get("title") or meta.get("customTitle") or meta.get("agentName") or meta.get("firstUserPrompt") or meta.get("sessionId") or "Untitled Session"
    lines = [
        f"# {title}",
        "",
        "## 会话元数据 (Session Metadata)",
        f"- **Session ID**: `{info.get('sessionId') or meta.get('sessionId')}`",
    ]
    if info.get("cliSessionId"):
        lines.append(f"- **CLI Session ID**: `{info.get('cliSessionId')}`")
    if info.get("sourceType"):
        lines.append(f"- **数据来源 (Source)**: `{info.get('sourceType')}`")
    if info.get("projectPath") or meta.get("cwd"):
        lines.append(f"- **项目文件夹 (Project Folder)**: `{info.get('projectPath') or meta.get('cwd')}`")
    if meta.get("cwd") and meta.get("cwd") != info.get("projectPath"):
        lines.append(f"- **工作目录 (Working Directory / Worktree)**: `{meta.get('cwd')}`")
    if meta.get("gitBranch") or info.get("gitBranch"):
        lines.append(f"- **Git 分支 (Branch)**: `{meta.get('gitBranch') or info.get('gitBranch')}`")
    if meta.get("model") or info.get("model"):
        lines.append(f"- **模型 (Model)**: `{meta.get('model') or info.get('model')}`")
    if info.get("accountUuid") or meta.get("ownerAccountUuid"):
        lines.append(f"- **原账号 UUID (Account)**: `{info.get('accountUuid') or meta.get('ownerAccountUuid')}`")
    if info.get("orgUuid") or meta.get("ownerOrganizationUuid"):
        lines.append(f"- **原组织 UUID (Organization)**: `{info.get('orgUuid') or meta.get('ownerOrganizationUuid')}`")
    lines.append(f"- **创建时间 (Created At)**: `{format_timestamp(info.get('createdAt') or meta.get('firstTimestamp'))}`")
    lines.append(f"- **最后活跃 (Updated At)**: `{format_timestamp(info.get('updatedAt') or meta.get('lastTimestamp'))}`")

    if meta.get("prLinks"):
        lines.append("\n### 关联 GitHub PR")
        for pr in meta["prLinks"]:
            lines.append(f"- PR #{pr.get('prNumber')}: {pr.get('prUrl')} ({pr.get('prRepository')})")

    if meta.get("frameLinks"):
        lines.append("\n### 关联 Artifact / 预览看板")
        for fl in meta["frameLinks"]:
            lines.append(f"- **{fl.get('title')}**: `{fl.get('path')}` ({fl.get('frameUrl')})")

    rec_files = parsed.get("reconstructedFiles", [])
    if rec_files:
        lines.append(f"\n### 会话中创建/修改的代码与文件 ({len(rec_files)} 个)")
        for rf in rec_files:
            lines.append(f"- `{rf.get('filePath')}` ({rf.get('lastOperation')}, {rf.get('size')} bytes)")

    lines.append("\n---\n")
    lines.append("## 多轮对话记录 (Conversation Transcript)\n")

    for idx, turn in enumerate(parsed.get("turns", []), 1):
        role = turn.get("role", "user")
        ts_str = turn.get("timestampFormatted", "")
        if role == "user":
            lines.append(f"### 👤 User (Turn {idx}) — `{ts_str}`\n")
        else:
            model_tag = f" [{turn.get('model')}]" if turn.get("model") else ""
            lines.append(f"### 🤖 Claude{model_tag} (Turn {idx}) — `{ts_str}`\n")

        for blk in turn.get("blocks", []):
            btype = blk.get("type")
            if btype == "text":
                lines.append(blk.get("text", ""))
                lines.append("")
            elif btype == "thinking":
                thinking_text = blk.get("thinking", "").strip()
                if thinking_text:
                    lines.append("<details><summary>💭 思考过程 (Thinking)</summary>\n")
                    lines.append(thinking_text)
                    lines.append("\n</details>\n")
            elif btype == "attachment":
                lines.append(f"> 📎 **附件 (Attachment)**: `{blk.get('fileName')}` ({blk.get('fileType')})")
                if blk.get("content"):
                    lines.append("```text")
                    lines.append(blk["content"][:5000])
                    lines.append("```\n")
            elif btype == "tool_use":
                tname = blk.get("name", "tool")
                tinput = blk.get("input", {})
                lines.append(f"#### 🛠️ 工具调用: `{tname}`")
                if tname in ("Write", "write_file", "create_file") and isinstance(tinput, dict):
                    fp = tinput.get("file_path") or tinput.get("path")
                    lines.append(f"- **写入文件**: `{fp}`")
                    lines.append("<details><summary>查看写入代码内容</summary>\n")
                    ext = os.path.splitext(str(fp))[1].lstrip(".") or "text"
                    lines.append(f"```{ext}")
                    lines.append(str(tinput.get("content") or tinput.get("file_text") or "")[:15000])
                    lines.append("```")
                    lines.append("</details>\n")
                elif tname in ("Edit", "replace", "str_replace") and isinstance(tinput, dict):
                    fp = tinput.get("file_path") or tinput.get("path")
                    lines.append(f"- **修改文件**: `{fp}`")
                    lines.append("<details><summary>查看代码变更 (Diff)</summary>\n")
                    lines.append("```diff")
                    for ol in str(tinput.get("old_string") or tinput.get("old_str") or "")[:5000].splitlines():
                        lines.append(f"- {ol}")
                    for nl in str(tinput.get("new_string") or tinput.get("new_str") or "")[:5000].splitlines():
                        lines.append(f"+ {nl}")
                    lines.append("```")
                    lines.append("</details>\n")
                elif tname == "Bash" and isinstance(tinput, dict):
                    lines.append("```bash")
                    lines.append(str(tinput.get("command", "")))
                    lines.append("```")
                else:
                    lines.append("```json")
                    lines.append(json.dumps(tinput, ensure_ascii=False, indent=2)[:4000])
                    lines.append("```")

                tresult = blk.get("result")
                if isinstance(tresult, dict) and tresult.get("content"):
                    err_tag = " (ERROR)" if tresult.get("is_error") else ""
                    lines.append(f"<details><summary>📤 工具执行结果{err_tag}</summary>\n")
                    lines.append("```text")
                    lines.append(str(tresult["content"])[:5000])
                    lines.append("```")
                    lines.append("</details>\n")
            elif btype == "tool_result":
                lines.append(f"<details><summary>📤 工具结果: `{blk.get('name', '')}`</summary>\n")
                lines.append("```text")
                lines.append(str(blk.get("content", ""))[:5000])
                lines.append("```")
                lines.append("</details>\n")

        lines.append("---\n")

    return "\n".join(lines)
