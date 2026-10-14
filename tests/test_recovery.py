#!/usr/bin/env python3
"""
Self-contained, desensitized unit & integration test suite for Claude 1:1 Recovery Suite.
Uses temporary synthetic fixtures so it runs cleanly on any developer's machine.
"""

import gzip
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_recovery.extractor import (
    EOF_BYTES,
    HDR_MAGIC,
    encode_cwd_for_claude_projects,
    extract_chromium_simple_cache_entry,
    run_full_recovery,
    snappy_decompress,
)
from claude_recovery.transcript_parser import (
    normalize_base_project_path,
    parse_cloud_conversation,
    parse_jsonl_full,
    render_conversation_to_markdown,
)
from migrate_account import discover_claude_desktop_scopes, migrate_scopes_to_target


class TestClaudeRecoverySuite(unittest.TestCase):
    def test_cwd_encoding_and_worktree_normalization(self) -> None:
        wt_cwd = "/Users/dev/workspace/demo-project/.claude/worktrees/feature-branch-123"
        encoded = encode_cwd_for_claude_projects(wt_cwd)
        self.assertEqual(
            encoded,
            "-Users-dev-workspace-demo-project--claude-worktrees-feature-branch-123",
        )
        base = normalize_base_project_path(wt_cwd)
        self.assertEqual(base, "/Users/dev/workspace/demo-project")

    def test_snappy_decompression(self) -> None:
        # Uncompressed length = 11 ("hello world"), literal tag = (10 << 2) | 0 = 40 (0x28)
        payload = b"\x0b\x28hello world"
        self.assertEqual(snappy_decompress(payload), b"hello world")

    def test_chromium_simple_cache_parser_and_crc32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = os.path.join(tmp, "1234567890abcdef_0")
            key = b"1/0/https://claude.ai/api/organizations/org-1/chat_conversations/conv-1?tree=True"
            raw_json = json.dumps({"uuid": "conv-1", "name": "Test Cloud Chat", "chat_messages": []}).encode("utf-8")
            compressed_body = gzip.compress(raw_json)
            crc = zlib.crc32(compressed_body) & 0xFFFFFFFF

            header = struct.pack("<QIIII", HDR_MAGIC, 5, len(key), 0, 0)
            eof1 = struct.pack("<QIIII", struct.unpack("<Q", EOF_BYTES)[0], 1, crc, 0, 0)
            eof0 = struct.pack("<QIIII", struct.unpack("<Q", EOF_BYTES)[0], 1, 0, 0, 0)

            with open(cache_file, "wb") as f:
                f.write(header + key + compressed_body + eof1 + eof0)

            parsed_key, body, crc_ok = extract_chromium_simple_cache_entry(cache_file)
            self.assertTrue(crc_ok)
            self.assertIn("https://claude.ai/api/organizations/org-1", parsed_key or "")
            self.assertEqual(json.loads(body.decode("utf-8"))["name"], "Test Cloud Chat")

    def test_jsonl_parser_and_code_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = os.path.join(tmp, "session-1.jsonl")
            records = [
                {
                    "type": "custom-title",
                    "customTitle": "Build API Server",
                    "sessionId": "session-1",
                },
                {
                    "type": "file-history-snapshot",
                    "snapshot": {
                        "trackedFileBackups": {
                            "src/main.py": {
                                "backupFileName": "abcd1234@v2",
                                "version": 2,
                                "realParentDir": "/Users/dev/demo/src",
                                "backupTime": "2026-10-01T10:00:00Z",
                            }
                        }
                    },
                },
                {
                    "type": "user",
                    "uuid": "u-1",
                    "timestamp": "2026-10-01T10:00:00Z",
                    "cwd": "/Users/dev/demo",
                    "gitBranch": "main",
                    "message": {"role": "user", "content": "Create a hello world script in app.py"},
                },
                {
                    "type": "assistant",
                    "uuid": "a-1",
                    "timestamp": "2026-10-01T10:00:05Z",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-opus-4-6",
                        "content": [
                            {"type": "thinking", "thinking": "I will write app.py and then update it."},
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "Write",
                                "input": {"file_path": "/Users/dev/demo/app.py", "content": "print('hello')\n"},
                            },
                            {
                                "type": "tool_use",
                                "id": "tu-2",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/Users/dev/demo/app.py",
                                    "old_string": "hello",
                                    "new_string": "hello world",
                                },
                            },
                            {"type": "text", "text": "Done creating and updating `app.py`."},
                        ],
                    },
                },
            ]
            with open(jsonl_path, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

            parsed = parse_jsonl_full(jsonl_path)
            self.assertEqual(parsed["meta"]["customTitle"], "Build API Server")
            self.assertIn("abcd1234@v2", parsed["meta"]["trackedFileBackups"])
            self.assertEqual(len(parsed["turns"]), 2)
            self.assertEqual(len(parsed["reconstructedFiles"]), 1)
            self.assertEqual(parsed["reconstructedFiles"][0]["content"], "print('hello world')\n")

            md = render_conversation_to_markdown(parsed, {"title": "Build API Server"})
            self.assertIn("# Build API Server", md)
            self.assertIn("hello world", md)

    def test_end_to_end_recovery_and_account_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = os.path.join(tmp, "ClaudeApp")
            cli_dir = os.path.join(tmp, "dot_claude")
            out_dir = os.path.join(tmp, "recovered")

            # Create synthetic banned account scope
            banned_ccs = os.path.join(app_dir, "claude-code-sessions", "acc-old", "org-old")
            banned_lam = os.path.join(app_dir, "local-agent-mode-sessions", "acc-old", "org-old")
            os.makedirs(banned_ccs, exist_ok=True)
            os.makedirs(banned_lam, exist_ok=True)

            with open(os.path.join(banned_ccs, "local_s1.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "sessionId": "local_s1",
                        "cliSessionId": "cli-uuid-1",
                        "title": "Demo Recovery Session",
                        "cwd": "/Users/dev/project-alpha",
                        "createdAt": 1780000000000,
                    },
                    f,
                )

            with open(os.path.join(banned_lam, "spaces.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "spaces": [
                            {
                                "id": "space-1",
                                "name": "Alpha Space",
                                "folders": [{"path": "/Users/dev/project-alpha"}],
                                "instructions": "Follow clean architecture",
                            }
                        ]
                    },
                    f,
                )

            # Create synthetic ~/.claude/projects transcript
            enc_proj = os.path.join(cli_dir, "projects", "-Users-dev-project-alpha")
            os.makedirs(enc_proj, exist_ok=True)
            with open(os.path.join(enc_proj, "cli-uuid-1.jsonl"), "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "user",
                            "timestamp": "2026-09-01T00:00:00Z",
                            "cwd": "/Users/dev/project-alpha",
                            "message": {"role": "user", "content": "Hello recovery"},
                        }
                    )
                    + "\n"
                )

            catalog = run_full_recovery(out_dir, claude_app_dir=app_dir, claude_cli_dir=cli_dir)
            self.assertEqual(catalog["summary"]["totalCodeProjects"], 1)
            self.assertEqual(catalog["summary"]["totalCodeTranscripts"], 1)
            self.assertEqual(catalog["summary"]["totalCoworkSpaces"], 1)

            # Test migration to a new account scope
            scopes = discover_claude_desktop_scopes(app_dir)
            self.assertEqual(len(scopes), 1)
            mig_res = migrate_scopes_to_target(app_dir, "acc-new", "org-new", dry_run=False)
            self.assertEqual(mig_res["copiedCodeSessions"], 1)
            self.assertEqual(mig_res["mergedSpaces"], 1)
            self.assertTrue(
                os.path.exists(os.path.join(app_dir, "claude-code-sessions", "acc-new", "org-new", "local_s1.json"))
            )


if __name__ == "__main__":
    unittest.main()
