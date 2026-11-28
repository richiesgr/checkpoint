# AI interaction log

Unedited transcripts of the AI-assisted work on this project go in this folder.
Remove only secrets and personal data, and replace each with `[REDACTED]`.
Do not otherwise edit, summarise or reorder a transcript.

## File naming

`<tool>-YYYY-MM-DD-<topic>.md`

| Tool | Example |
|------|---------|
| Kiro | `kiro-2025-01-15-spec-requirements.md` |
| Claude | `claude-2025-01-15-design-review.md` |
| ChatGPT | `chatgpt-2025-01-15-kafka-keys.md` |
| Copilot | `copilot-2025-01-15-tests.md` |
| Cursor | `cursor-2025-01-15-refactor.md` |

Keep raw exports (JSON, HTML) next to the Markdown file under the same base name.

## Kiro sessions on this project

`kiro-<date>-<topic>-<session id>.md` and the matching `.jsonl` are the complete history of every
Kiro session that worked in this workspace: the spec sessions that built tasks 1 to 11 (24
November) and the orchestrator session that did task 12 onward (26 to 28 November). Exported with
`scripts/export_ai_log.py`, which reads Kiro's own session store
(`~/.kiro/sessions/*/<session id>/`) and writes both forms, including any sub-agent runs. Only
secrets and personal data are substituted, each with `[REDACTED]`: the Windows user name, private
IPv4 addresses, e-mail addresses, and anything shaped like a credential (private key blocks,
GitHub/AWS/OpenAI-style tokens, the user's SSH private key path). Nothing else is edited,
summarised or reordered. Re-run the script to refresh the export after further sessions:

```powershell
.\.venv\Scripts\python.exe scripts\export_ai_log.py
```
