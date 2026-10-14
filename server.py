#!/usr/bin/env python3
"""
Local HTTP Server for the 1:1 Claude Desktop Recovery & Replay Web GUI (server.py).
Provides instant browsing of all recovered Code Projects, Cowork Spaces, Cloud Chats,
Subagent transcripts, live project folder files, and AI-reconstructed code artifacts.
"""

import argparse
import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

from claude_recovery.extractor import get_default_claude_app_dir, run_full_recovery
from claude_recovery.transcript_parser import (
    parse_cloud_conversation,
    parse_jsonl_full,
    render_conversation_to_markdown,
)
from migrate_account import discover_claude_desktop_scopes, migrate_scopes_to_target
from recover_claude import export_project_folders_to_disk

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RECOVERED_DIR = os.path.join(PROJECT_ROOT, "recovered_data")
WEB_DIR = os.path.join(PROJECT_ROOT, "web")
CLAUDE_APP_DIR = get_default_claude_app_dir()

IGNORED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".cache",
    ".idea",
    ".DS_Store",
}


def list_local_project_files(folder_path: str, max_files: int = 300) -> List[Dict[str, Any]]:
    """List files in a local project folder for the GUI File Explorer."""
    if not folder_path or not os.path.isdir(folder_path):
        return []
    result: List[Dict[str, Any]] = []
    try:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in sorted(dirs) if d not in IGNORED_DIRS and not d.startswith(".claude")]
            for fn in sorted(files):
                if fn == ".DS_Store":
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, folder_path)
                try:
                    st = os.stat(fp)
                    result.append({
                        "name": fn,
                        "relPath": rel,
                        "fullPath": fp,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime * 1000),
                    })
                except Exception:
                    pass
                if len(result) >= max_files:
                    return result
    except Exception:
        pass
    return result


class RecoveryRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep console output clean
        pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            index_file = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(index_file):
                data = open(index_file, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json({"error": "web/index.html not found"}, 404)
            return

        if path == "/api/catalog":
            cat_path = os.path.join(RECOVERED_DIR, "catalog.json")
            if not os.path.exists(cat_path):
                cat = run_full_recovery(RECOVERED_DIR)
            else:
                cat = json.load(open(cat_path, "r", encoding="utf-8"))
            self._send_json(cat)
            return

        if path == "/api/scopes":
            scopes = discover_claude_desktop_scopes(CLAUDE_APP_DIR)
            self._send_json({"scopes": scopes})
            return

        if path == "/api/session":
            stype = qs.get("type", ["code"])[0]
            tpath = qs.get("path", [""])[0]
            if not tpath or not os.path.exists(tpath):
                self._send_json({"error": f"Transcript file not found: {tpath}"}, 404)
                return
            try:
                if stype == "cloud" or tpath.endswith(".json"):
                    cobj = json.load(open(tpath, "r", encoding="utf-8"))
                    res = parse_cloud_conversation(cobj)
                else:
                    res = parse_jsonl_full(tpath)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/api/export_md":
            stype = qs.get("type", ["code"])[0]
            tpath = qs.get("path", [""])[0]
            title = qs.get("title", ["conversation"])[0]
            if not tpath or not os.path.exists(tpath):
                self._send_json({"error": "Transcript file not found"}, 404)
                return
            try:
                if stype == "cloud" or tpath.endswith(".json"):
                    cobj = json.load(open(tpath, "r", encoding="utf-8"))
                    parsed_conv = parse_cloud_conversation(cobj)
                else:
                    parsed_conv = parse_jsonl_full(tpath)
                md = render_conversation_to_markdown(parsed_conv, {"title": title})
                raw = md.encode("utf-8")
                safe_fn = urllib.parse.quote(f"{title[:50]}.md")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{safe_fn}")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/api/project_files":
            folder = qs.get("path", [""])[0]
            files = list_local_project_files(folder)
            self._send_json({"folder": folder, "exists": os.path.isdir(folder), "files": files})
            return

        if path == "/api/file_content":
            fpath = qs.get("path", [""])[0]
            download = qs.get("download", ["0"])[0] == "1"
            if not fpath or not os.path.isfile(fpath):
                self._send_json({"error": "File not found"}, 404)
                return
            try:
                ctype, _ = mimetypes.guess_type(fpath)
                if not ctype:
                    ctype = "application/octet-stream"
                if download or ctype.startswith(("image/", "application/pdf", "video/", "audio/")) or fpath.endswith((".docx", ".xlsx", ".pptx", ".zip", ".bin")):
                    data = open(fpath, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    if download:
                        fn = urllib.parse.quote(os.path.basename(fpath))
                        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fn}")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(300_000)
                    self._send_json({
                        "path": fpath,
                        "name": os.path.basename(fpath),
                        "size": os.path.getsize(fpath),
                        "content": content,
                    })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/rescan":
            cat = run_full_recovery(RECOVERED_DIR)
            export_project_folders_to_disk(cat, RECOVERED_DIR, export_all_md=True)
            with open(os.path.join(RECOVERED_DIR, "catalog.json"), "w", encoding="utf-8") as cf:
                json.dump(cat, cf, ensure_ascii=False, indent=2)
            self._send_json({"ok": True, "summary": cat["summary"]})
            return

        if path == "/api/migrate":
            target = payload.get("targetScope", "").strip()
            dry_run = bool(payload.get("dryRun", False))
            if "/" not in target:
                self._send_json({"error": "targetScope must be <accountUuid>/<orgUuid>"}, 400)
                return
            acc, org = target.split("/", 1)
            res = migrate_scopes_to_target(CLAUDE_APP_DIR, acc, org, dry_run=dry_run)
            self._send_json({"ok": True, "result": res})
            return

        self._send_json({"error": "Not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the 1:1 Claude Recovery Web GUI Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), RecoveryRequestHandler)
    print(f"============================================================")
    print(f" 🚀 Claude 1:1 数据复原与防封重放工作台已启动")
    print(f" 👉 访问地址: http://{args.host}:{args.port}")
    print(f" 📂 归档目录: {RECOVERED_DIR}")
    print(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
