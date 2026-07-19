"""Locate the repo root, the rules file, and the session transcript."""

from __future__ import annotations

import os
import re
from typing import List, Optional


def encode_cwd(path: str) -> str:
    """Absolute path with every non-alphanumeric char replaced by '-'.

    /Users/me/proj -> -Users-me-proj  (matches the on-disk project dir name).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def project_transcript_dir(cwd: str) -> str:
    return os.path.join(os.path.expanduser("~/.claude/projects"), encode_cwd(cwd))


def list_sessions(cwd: str) -> List[str]:
    d = project_transcript_dir(cwd)
    if not os.path.isdir(d):
        return []
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files


def newest_session(cwd: str) -> Optional[str]:
    files = list_sessions(cwd)
    return files[0] if files else None


def find_repo_root(start: str) -> str:
    """Walk up to the nearest .git; fall back to start."""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


def code_extension_mix(repo_root: str, limit: int = 4000) -> dict:
    """Count source files by extension (bounded walk), skipping vendor dirs."""
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
            ".cellamind", "target", ".next", "vendor"}
    counts: dict = {}
    seen = 0
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lstrip(".").lower()
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
                seen += 1
                if seen >= limit:
                    return counts
    return counts
