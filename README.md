# Cursor to Codex

[English](README.md) | [简体中文](README.zh-CN.md)

A Codex Skill for migrating selected local Cursor conversations into Codex tasks under a chosen local project.

## What it does

- reads Cursor's local conversation index in read-only mode;
- finds matching JSONL transcripts;
- preserves titles, IDs, source paths, user requests, and compact historical context;
- redacts common credentials before creating new tasks;
- creates and verifies one Codex task per selected conversation;
- keeps the source Cursor data untouched.

This is context migration/reconstruction, not a byte-for-byte import of Cursor's chat UI history.

## Install

Copy this folder into a discoverable Codex skills directory, for example:

```text
<CODEX_HOME>/skills/cursor-to-codex/
```

The skill requires the Codex app task tools (`list_projects`, `create_thread`, and `wait_threads`) when creating tasks. The inspection script itself uses only Python's standard library.

## Test local discovery

```powershell
python scripts/inspect_cursor_conversations.py --format text
python scripts/inspect_cursor_conversations.py --query "KV cache" --format json
```

Use an exact `--conversation-id` or a project-root/title filter before migration. Review ambiguous matches before creating tasks.
