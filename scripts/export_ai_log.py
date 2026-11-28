"""Export the Kiro session history of this workspace to docs/ai-log/ (not Event_Path code).

For every Kiro session of the workspace, writes two files under the same base name
``kiro-YYYY-MM-DD-<topic>-<short id>``:

- ``.md``: the transcript in order: user prompts, assistant messages, tool calls with their
  arguments and results, approval prompts, and sub-agent runs (prompt, transcript, response).
- ``.jsonl``: the raw Kiro export (the session's ``messages.jsonl`` followed by its sub-agent
  files), so nothing is lost by the Markdown rendering.

Nothing is summarised, reordered or dropped. Only these substitutions are made, each replaced by
``[REDACTED]``: the Windows user name in paths, private IPv4 addresses, email addresses, and
anything that looks like a credential (private key blocks, GitHub/AWS/OpenAI-style tokens).

Usage:  .\\.venv\\Scripts\\python.exe scripts\\export_ai_log.py [--sessions-dir DIR]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "ai-log"
DEFAULT_SESSIONS = Path.home() / ".kiro" / "sessions"
REDACTED = "[REDACTED]"


def _redactor():
    user = re.escape(getpass.getuser())
    patterns = [
        # Windows user name in paths, in every escaping seen in JSON (incl. double-escaped
        # tool-output text) and Markdown: \, \\, \\\\, /, %5C, %2F.
        re.compile(rf"(?i)(users(?:\\\\\\\\|\\\\|\\|/|%5c|%2f))({user})(?=\\|/|%5c|%2f|\b)"),
        # pytest's temp folder name "pytest-of-<user>".
        re.compile(rf"(?i)(pytest-of-){user}\b"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        # The user's SSH private key file path (never the key contents, which are never printed).
        re.compile(r"F:(?:\\\\\\\\|\\\\|\\|/)Electronic(?:\\\\\\\\|\\\\|\\|/)keys(?:\\\\\\\\|\\\\|\\|/)id_ed25519\b(?!\.pub)"),
        re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b"),
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ]

    def redact(text: str) -> str:
        text = patterns[0].sub(lambda m: m.group(1) + REDACTED, text)
        for p in patterns[1:]:
            text = p.sub(REDACTED, text)
        return text

    return redact


redact = _redactor()


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "-".join(s.split("-")[:8]) or "session"


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, indent=2))
        return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _fence(body: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return f"{ticks}{lang}\n{body}\n{ticks}"


def _render(records: list[dict], sub_files: dict[str, Path], depth: int = 0) -> list[str]:
    h = "#" * (3 + depth)
    out: list[str] = []
    for rec in records:
        p, ts = rec.get("payload", {}), rec.get("timestamp", "")
        t = p.get("type")
        if t == "user":
            out += [f"{h} User ({ts})", "", _text(p.get("content")), ""]
            if p.get("documents"):
                out += ["Attached documents:", _fence(_text(p["documents"]), "json"), ""]
        elif t == "assistant":
            out += [f"{h} Assistant ({ts})", "", _text(p.get("content")), ""]
        elif t == "tool_call":
            out += [f"{h} Tool call: {p.get('toolName')} ({ts})", "", _fence(_text(p.get("args")), "json"), ""]
        elif t == "tool_result":
            status = "ok" if p.get("success", True) else "error"
            out += [f"{h} Tool result: {p.get('title') or p.get('toolCallId')} [{status}]", "",
                    _fence(_text(p.get("content"))), ""]
        elif t == "pending_interaction":
            out += [f"{h} Approval asked ({ts})", "", _text(p.get("question")), ""]
        elif t == "interaction_resolved":
            out += [f"{h} Approval answer: {p.get('outcome')} {_text(p.get('selectedOption'))}".rstrip(), ""]
        elif t == "sub_agent_start":
            out += [f"{h} Sub-agent started: {p.get('subAgentName')} ({ts})", "", _text(p.get("prompt")), ""]
            sub = sub_files.get(str(p.get("subSessionId")))
            if sub:
                out += [f"{h}# Sub-agent transcript", ""]
                out += _render(_read(sub), sub_files, depth + 1)
        elif t == "sub_agent_complete":
            out += [f"{h} Sub-agent finished: {p.get('status')}", "", _text(p.get("response") or p.get("errorMessage")), ""]
        elif t in ("agent_note", "session_start"):
            out += [f"{h} {t} ({ts})", "", _text(p.get("content")), ""]
    return out


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def export(sessions_dir: Path) -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    workspace = os.path.normcase(str(REPO))
    written: list[Path] = []
    for index_file in sessions_dir.glob("*/.index/index.json"):
        index = json.loads(index_file.read_text(encoding="utf-8"))
        for entry in index.get("entries", []):
            paths = [os.path.normcase(p) for p in entry.get("workspacePaths", [])]
            if workspace not in paths:
                continue
            sess_dir = index_file.parent.parent / entry["id"]
            messages = sess_dir / "messages.jsonl"
            if not messages.is_file():
                continue
            subs = {p.stem: p for p in sorted((sess_dir / "sub-executions").glob("*.jsonl"))}
            base = f"kiro-{entry['createdAt'][:10]}-{_slug(entry.get('title', ''))}-{entry['id'][5:13]}"

            raw = messages.read_text(encoding="utf-8")
            for sub in subs.values():
                raw += sub.read_text(encoding="utf-8")
            (OUT_DIR / f"{base}.jsonl").write_text(redact(raw), encoding="utf-8")

            header = [
                f"# {entry.get('title', entry['id'])}",
                "",
                f"- Tool: Kiro ({entry.get('agentMode', '')} mode)",
                f"- Session: `{entry['id']}`",
                f"- Started: {entry.get('createdAt')}, last change: {entry.get('lastModifiedAt')}",
                f"- Raw export: `{base}.jsonl`",
                "",
            ]
            body = _render(_read(messages), subs)
            (OUT_DIR / f"{base}.md").write_text(redact("\n".join(header + body)) + "\n", encoding="utf-8")
            written += [OUT_DIR / f"{base}.md", OUT_DIR / f"{base}.jsonl"]
    return sorted(written)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS)
    args = parser.parse_args()
    for path in export(args.sessions_dir):
        print(f"{path.stat().st_size:>10}  {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
