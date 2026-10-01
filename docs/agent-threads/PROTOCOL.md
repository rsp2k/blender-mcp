# Agent Thread Protocol

Async, immutable, flat-file messages between agents (or people). See ~/.claude/CLAUDE.md for the full protocol.

- One folder per conversation under `docs/agent-threads/{thread-name}/`.
- File name: `{YYYYMMDDTHHMMSSZ}-{from}-{2-4 word summary}.md` (UTC; get the prefix with `date -u +%Y%m%dT%H%M%SZ`).
- Never edit or rename an existing message; reply with a new file and reference the prior one by filename in `Re`.
- Put your session UUID in `From` (and the recipient's in `To` when known) so either conversation can be resumed with `claude --resume <uuid>`.
