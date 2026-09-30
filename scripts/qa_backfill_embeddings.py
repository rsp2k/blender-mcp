"""Embed history that predates QA_EMBEDDINGS: past tool errors and feedback.

fastmcp-feedback's EmbeddingSink only embeds records as they arrive. This
fills ffb_embeddings with what was already stored, in the sink's own text
format and keys, so later records of the same source dedupe against it:

- call_error: "{tool}: {error_type}: {error_message}", keyed by the call id
  (calls before the outcome column existed are picked by their error text)
- feedback: "{title}\\n{body}", keyed by the feedback shortcode

Run inside the server container (it reads DATABASE_URL, GPU_API_KEY and the
QA_EMBED_* settings the server uses); safe to re-run, existing rows are kept:

    docker exec blender-mcp-server-prod python /app/scripts/qa_backfill_embeddings.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

from fastmcp_feedback.instrumentation.embeddings import EmbeddingText, OpenAIEmbedder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BATCH = 32

ERRORS_SQL = text("""
    select id, tool, error_type, error_message from ffb_tool_calls
    where coalesce(error_message, '') <> '' or outcome in ('error', 'soft_error')
""")
FEEDBACK_SQL = text("select id, title, body from feedback")
INSERT_SQL = text("""
    insert into ffb_embeddings
        (id, source_type, source_id, model, dim, text_hash, text, created_at, embedding)
    values (:id, :source_type, :source_id, :model, :dim, :text_hash, :text, :created_at,
            cast(:embedding as vector))
    on conflict (source_type, source_id, model) do nothing
""")


def _clean(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    embedder = OpenAIEmbedder(
        base_url=os.environ.get("QA_EMBED_BASE_URL", "https://blender-mcp.gpu.supported.systems/v1"),
        api_key=os.environ["GPU_API_KEY"],
        model=os.environ.get("QA_EMBED_MODEL", "mxbai-embed-large"),
    )
    async with engine.connect() as conn:
        errors = (await conn.execute(ERRORS_SQL)).all()
        feedback = (await conn.execute(FEEDBACK_SQL)).all()
    items: list[EmbeddingText] = []
    for r in errors:
        parts = [r.tool, r.error_type, _clean(r.error_message)]
        items.append(EmbeddingText("call_error", r.id, ": ".join(p for p in parts if p)))
    for r in feedback:
        body = "\n".join(t for t in (_clean(r.title), _clean(r.body)) if t)
        if body:
            items.append(EmbeddingText("feedback", r.id, body))
    print(f"backfill: {len(errors)} errors, {len(feedback)} feedback -> {len(items)} texts")

    written = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        vectors = await embedder.embed([t.text for t in batch])
        now = datetime.now(UTC)
        rows = [{
            "id": str(uuid.uuid4()), "source_type": t.source_type, "source_id": t.source_id,
            "model": embedder.model, "dim": len(v), "text_hash": t.text_hash, "text": t.text,
            "created_at": now, "embedding": "[" + ",".join(f"{x:.7g}" for x in v) + "]",
        } for t, v in zip(batch, vectors, strict=True)]
        async with engine.begin() as conn:
            res = await conn.execute(INSERT_SQL, rows)
            written += res.rowcount or 0
    print(f"backfill: wrote {written} new embeddings")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
