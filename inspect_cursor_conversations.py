#!/usr/bin/env python3
"""Read-only discovery of local Cursor conversation titles and transcripts.

The script emits a migration manifest. It never writes to Cursor storage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
]


def default_db() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cursor" / "User" / "globalStorage" / "conversation-search.db"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "conversation-search.db"


def default_transcript_root() -> Path:
    return Path.home() / ".cursor" / "projects"


def redact(value: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def text_from_message(obj: dict[str, Any]) -> str:
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)


def clean_user_text(value: str) -> str:
    value = re.sub(r"<timestamp>.*?</timestamp>\s*", "", value, flags=re.S)
    value = re.sub(r"<user_query>\s*", "", value)
    value = re.sub(r"\s*</user_query>\s*", "", value)
    return redact(value).strip()


def transcript_paths(root: Path, conversation_id: str) -> list[Path]:
    if not root.exists():
        return []
    exact_name = f"{conversation_id}.jsonl"
    return sorted(p for p in root.rglob(exact_name) if p.is_file())


def extract_transcript(path: Path, max_user_messages: int, max_chars: int) -> dict[str, Any]:
    users: list[str] = []
    assistant_texts: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role")
        text = text_from_message(obj)
        if not text:
            continue
        if role == "user":
            cleaned = clean_user_text(text)
            if cleaned:
                users.append(cleaned)
        elif role == "assistant":
            cleaned = redact(text).strip()
            if cleaned:
                assistant_texts.append(cleaned)
    return {
        "user_messages": users[:max_user_messages],
        "assistant_tail": [text[:2000] for text in assistant_texts[-2:]],
        "transcript_bytes": path.stat().st_size,
        "summary_text": summarize(users, assistant_texts, max_chars),
    }


def summarize(users: list[str], assistants: list[str], max_chars: int) -> str:
    parts: list[str] = []
    for text in users[:5]:
        parts.append(f"User request: {text}")
    for text in assistants[-1:]:
        compact = re.sub(r"\s+", " ", text)
        parts.append(f"Reported historical assistant context: {compact}")
    result = "\n".join(parts)
    return result[:max_chars]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db())
    parser.add_argument("--transcript-root", type=Path, default=default_transcript_root())
    parser.add_argument("--project-root", type=str, help="Only keep transcripts containing this path")
    parser.add_argument("--query", action="append", default=[], help="Case-insensitive substring matched against title or transcript")
    parser.add_argument("--conversation-id", action="append", default=[])
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--max-user-messages", type=int, default=8)
    parser.add_argument("--max-summary-chars", type=int, default=6000)
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args()


def load_rows(db: Path) -> list[dict[str, Any]]:
    if not db.exists():
        raise FileNotFoundError(f"Cursor conversation index not found: {db}")
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
        required = {"id", "title", "updated_at", "is_archived"}
        if not required.issubset(columns):
            raise RuntimeError("Unsupported Cursor conversation index: missing conversations columns")
        rows = connection.execute(
            "SELECT id, title, updated_at, is_archived, source, scope FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def matches(row: dict[str, Any], transcript_text: str, args: argparse.Namespace) -> bool:
    if args.conversation_id and row["id"] not in args.conversation_id:
        return False
    if not args.include_archived and int(row.get("is_archived") or 0):
        return False
    haystack = f"{row.get('title', '')}\n{transcript_text}".lower()
    return all(query.lower() in haystack for query in args.query)


def contains_project_root(transcript_text: str, project_root: str) -> bool:
    """Match both literal and JSON-escaped Windows paths in JSONL text."""
    needle = project_root.lower().rstrip("\\/")
    normalized = transcript_text.replace("\\\\", "\\").lower()
    return needle in transcript_text.lower() or needle in normalized


def main() -> int:
    args = parse_args()
    try:
        rows = load_rows(args.db)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

    project_root = args.project_root.lower() if args.project_root else None
    manifest: list[dict[str, Any]] = []
    for row in rows:
        paths = transcript_paths(args.transcript_root, row["id"])
        transcript_text = ""
        selected_path: Path | None = paths[0] if paths else None
        if selected_path:
            transcript_text = selected_path.read_text(encoding="utf-8", errors="replace")
        if project_root and not contains_project_root(transcript_text, project_root):
            continue
        if not matches(row, transcript_text, args):
            continue
        record: dict[str, Any] = {
            "conversation_id": row["id"],
            "title": redact(str(row.get("title") or "")),
            "updated_at": datetime.fromtimestamp(int(row["updated_at"]) / 1000, tz=timezone.utc).isoformat(),
            "is_archived": bool(row.get("is_archived")),
            "source": row.get("source"),
            "scope": row.get("scope"),
            "transcript_path": str(selected_path) if selected_path else None,
            "transcript_candidates": [str(path) for path in paths],
        }
        if selected_path:
            record.update(extract_transcript(selected_path, args.max_user_messages, args.max_summary_chars))
        else:
            record.update({"user_messages": [], "assistant_tail": [], "summary_text": "", "transcript_bytes": None})
        manifest.append(record)

    if args.format == "json":
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        for item in manifest:
            print(f"[{item['conversation_id']}] {item['title']}")
            print(f"  updated: {item['updated_at']} archived: {item['is_archived']}")
            print(f"  transcript: {item['transcript_path'] or '(not found)'}")
            for message in item.get("user_messages", [])[:3]:
                print(f"  user: {message[:500]}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
