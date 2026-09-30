"""Embeddings wiring (fastmcp-feedback 2026.9.30): the EmbeddingSink is added
only with QA_EMBEDDINGS on, chat LLM text reaches llm.call without image data,
and feedback submissions become feedback.submitted events. No network."""

import base64
import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from mcp.types import (
    ImageContent,
    SamplingMessage,
    TextContent,
    ToolResultContent,
    ToolUseContent,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from blender_mcp import feedback_tools, instrumentation
from blender_mcp.chat.config import ChatConfig
from blender_mcp.chat.providers import Backend
from blender_mcp.chat.routing import (
    PROMPT_MAX_CHARS,
    RoutingSamplingHandler,
    prompt_text,
)
from blender_mcp.storage.models import FeedbackCategory

IMAGE_B64 = base64.b64encode(b"\x89PNG" + bytes(range(256)) * 8).decode()


class FakeEmbedder:
    """Bag-of-letters vectors: deterministic, no network."""

    model = "fake-embed"
    dim = instrumentation.FFB_EMBEDDING_DIM

    def __init__(self):
        self.seen: list[str] = []

    async def embed(self, texts):
        self.seen.extend(texts)
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for ch in text.lower():
                v[ord(ch) % self.dim] += 1.0
            out.append(v)
        return out


class FakeMiddleware:
    def __init__(self, capture=True, fail=False):
        self.capture_llm_text = capture
        self.fail = fail
        self.llm_calls: list[dict] = []
        self.events: list[tuple] = []

    def record_llm_call(self, **kw):
        self.llm_calls.append(kw)
        return True

    def record_event(self, kind, **kw):
        if self.fail:
            raise RuntimeError("sink exploded")
        self.events.append((kind, kw))
        return True


def _clear_env(monkeypatch):
    for name in ("QA_EMBEDDINGS", "QA_EMBED_BASE_URL", "QA_EMBED_MODEL", "GPU_API_KEY",
                 "QA_LOG_STDERR"):
        monkeypatch.delenv(name, raising=False)


# ---- which sinks ------------------------------------------------------------

def _db_sink():
    from fastmcp_feedback.instrumentation import DatabaseSink

    return DatabaseSink(create_async_engine("sqlite+aiosqlite:///:memory:"),
                        prefix=instrumentation.FFB_TABLE_PREFIX,
                        embedding_dim=instrumentation.FFB_EMBEDDING_DIM)


def test_embeddings_off_by_default(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GPU_API_KEY", "gw-secret")
    assert instrumentation.embedding_sink(_db_sink()) is None


def test_embeddings_need_the_gateway_key(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("QA_EMBEDDINGS", "on")
    assert instrumentation.embedding_sink(_db_sink()) is None


def test_embeddings_on_uses_env_defaults(monkeypatch):
    from fastmcp_feedback.instrumentation import EmbeddingSink, OpenAIEmbedder

    _clear_env(monkeypatch)
    monkeypatch.setenv("QA_EMBEDDINGS", "on")
    monkeypatch.setenv("GPU_API_KEY", "gw-secret")
    db = _db_sink()
    sink = instrumentation.embedding_sink(db)
    assert isinstance(sink, EmbeddingSink) and sink.database_sink is db
    assert isinstance(sink.embedder, OpenAIEmbedder)
    assert sink.embedder.base_url == instrumentation.DEFAULT_EMBED_BASE_URL
    assert sink.embedder.model == "mxbai-embed-large"
    assert sink.embedder.api_key == "gw-secret"
    assert sink.embedder.dim == 1024


def test_embed_url_and_model_come_from_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("QA_EMBEDDINGS", "1")
    monkeypatch.setenv("GPU_API_KEY", "k")
    monkeypatch.setenv("QA_EMBED_BASE_URL", "https://embed.example/v1/")
    monkeypatch.setenv("QA_EMBED_MODEL", "other-1024")
    sink = instrumentation.embedding_sink(_db_sink())
    assert sink.embedder.base_url == "https://embed.example/v1"
    assert sink.embedder.model == "other-1024"


@pytest.mark.parametrize("embeddings", ["on", "off"])
async def test_install_adds_the_sink_only_when_enabled(monkeypatch, embeddings):
    from fastmcp_feedback.instrumentation import DatabaseSink, EmbeddingSink

    from blender_mcp import storage

    _clear_env(monkeypatch)
    monkeypatch.setenv("QA_LOG", "meta")
    monkeypatch.setenv("QA_EMBEDDINGS", embeddings)
    monkeypatch.setenv("GPU_API_KEY", "gw-secret")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(storage, "get_engine", lambda: engine)

    mw = instrumentation.install(FastMCP("t"))
    try:
        kinds = [type(s) for s in mw.dispatcher.sinks]
        assert kinds[0] is DatabaseSink
        assert (EmbeddingSink in kinds) is (embeddings == "on")
        assert mw.capture_llm_text is True
        if embeddings == "on":
            # The middleware hands its redactor to the sink.
            assert mw.embedding_sink.redactor is mw.redactor
            assert mw.embedding_sink.database_sink is mw.dispatcher.sinks[0]
    finally:
        await mw.aclose()
        await engine.dispose()


# ---- prompt text ------------------------------------------------------------

def _image():
    return ImageContent(type="image", data=IMAGE_B64, mimeType="image/png")


def test_prompt_is_the_last_user_turn_without_image_data():
    msgs = [
        SamplingMessage(role="user", content=TextContent(type="text", text="old question")),
        SamplingMessage(role="assistant", content=TextContent(type="text", text="old answer")),
        SamplingMessage(role="user", content=[TextContent(type="text", text="what is this?"),
                                              _image()]),
    ]
    text = prompt_text(msgs)
    assert text == "what is this?\n[image]"
    assert IMAGE_B64 not in text and IMAGE_B64[:40] not in text


def test_prompt_summarises_the_newest_tool_results():
    msgs = [
        SamplingMessage(role="user", content=TextContent(type="text", text="add a cube")),
        SamplingMessage(role="assistant", content=ToolUseContent(
            type="tool_use", id="t1", name="blender_execute_code", input={"code": "x"})),
        SamplingMessage(role="user", content=ToolResultContent(
            type="tool_result", toolUseId="t1",
            content=[TextContent(type="text", text="created Cube " + "y" * 1000), _image()])),
    ]
    text = prompt_text(msgs)
    lines = text.splitlines()
    assert lines[0] == "add a cube" and lines[1] == "tool results:"
    assert lines[2].startswith("- created Cube ") and len(lines[2]) <= 302
    assert IMAGE_B64[:40] not in text


def test_prompt_masks_inline_base64_and_is_bounded():
    blob = f"data:image/png;base64,{IMAGE_B64}"
    msgs = [SamplingMessage(role="user", content=TextContent(
        type="text", text=f"see {blob} and {IMAGE_B64} " + "z" * 10_000))]
    text = prompt_text(msgs)
    assert IMAGE_B64[:40] not in text and "[base64]" in text
    assert len(text) <= PROMPT_MAX_CHARS


def test_prompt_of_nothing_is_none():
    assert prompt_text([]) is None
    assert prompt_text(None) is None
    assert prompt_text([SamplingMessage(role="user", content=_image())]) is None


def test_prompt_accepts_plain_dict_messages():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"},
                                          {"type": "image", "data": IMAGE_B64}]}]
    assert prompt_text(msgs) == "hello\n[image]"


# ---- RoutingSamplingHandler.complete ----------------------------------------

def _gateway(reply):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                                         "usage": {"prompt_tokens": 9, "completion_tokens": 2}})
    return httpx.MockTransport(handler)


def _cfg():
    return ChatConfig(enabled=True, max_tokens=2048)


async def test_complete_passes_prompt_and_completion(monkeypatch):
    mw = FakeMiddleware(capture=True)
    monkeypatch.setattr(instrumentation, "middleware", lambda ctx=None: mw)
    handler = RoutingSamplingHandler(config_loader=_cfg, transport=_gateway("a red cube"))
    msgs = [SamplingMessage(role="user", content=[TextContent(type="text", text="describe"),
                                                  _image()])]
    await handler.complete(Backend("gateway", "qwen2.5vl", "https://gw.example/v1", "gw-secret"),
                           "system prompt", msgs, None)
    [rec] = mw.llm_calls
    assert rec["prompt"] == "describe\n[image]"
    assert rec["completion"] == "a red cube"
    assert "system prompt" not in rec["prompt"]
    assert IMAGE_B64[:40] not in repr(rec) and "gw-secret" not in repr(rec)


async def test_complete_builds_no_text_when_capture_is_off(monkeypatch):
    mw = FakeMiddleware(capture=False)
    monkeypatch.setattr(instrumentation, "middleware", lambda ctx=None: mw)
    handler = RoutingSamplingHandler(config_loader=_cfg, transport=_gateway("ok"))
    msgs = [SamplingMessage(role="user", content=TextContent(type="text", text="hi"))]
    await handler.complete(Backend("gateway", "qwen3", "https://gw.example/v1"), None, msgs, None)
    [rec] = mw.llm_calls
    assert rec["prompt"] is None and rec["completion"] is None


# ---- feedback.submitted -----------------------------------------------------

def _row():
    return SimpleNamespace(id="bug-AbC123", title="Render hangs", body="Cycles never finishes",
                           category=FeedbackCategory.bug)


def test_record_submitted_sends_the_event():
    mw = FakeMiddleware()
    ctx = SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=mw))
    feedback_tools._record_submitted(ctx, _row(), "user-1")
    [(kind, kw)] = mw.events
    assert kind == "feedback.submitted" and kw["key"] == "bug-AbC123"
    assert kw["attrs"] == {"title": "Render hangs", "description": "Cycles never finishes",
                           "type": "bug"}
    assert kw["user_sub"] == "user-1"


def test_record_submitted_is_a_no_op_or_silent_when_it_cannot_record():
    feedback_tools._record_submitted(None, _row(), None)
    feedback_tools._record_submitted(SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=None)),
                                     _row(), None)
    broken = SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=FakeMiddleware(fail=True)))
    feedback_tools._record_submitted(broken, _row(), None)


async def test_submit_feedback_records_the_event(monkeypatch):
    mw = FakeMiddleware()
    ctx = SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=mw))
    stored = {}

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def create(session, **kw):
        stored.update(kw)
        return SimpleNamespace(id="bug-Zz9", title=kw["title"], body=kw["body"],
                               category=kw["category"])

    async def no_link(ctx, feedback_id):
        return None

    monkeypatch.setattr(feedback_tools, "_resolve_user_id", lambda ctx: "user-9")
    monkeypatch.setattr(feedback_tools, "get_session", session)
    monkeypatch.setattr(feedback_tools, "create_feedback", create)
    monkeypatch.setattr(feedback_tools, "_link_recent_calls", no_link)

    out = json.loads(await feedback_tools.BlenderFeedbackComponent().submit_feedback(
        title=" Render hangs ", body="forever", category="bug", ctx=ctx))
    assert out["status"] == "ok" and out["id"] == "bug-Zz9"
    [(kind, kw)] = mw.events
    assert kind == "feedback.submitted" and kw["key"] == "bug-Zz9"
    assert kw["attrs"] == {"title": "Render hangs", "description": "forever", "type": "bug"}


async def test_rejected_feedback_records_nothing(monkeypatch):
    mw = FakeMiddleware()
    ctx = SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=mw))
    monkeypatch.setattr(feedback_tools, "_resolve_user_id", lambda ctx: "user-9")
    out = json.loads(await feedback_tools.BlenderFeedbackComponent().submit_feedback(
        title="", body="", category="bug", ctx=ctx))
    assert out["status"] == "error" and mw.events == []


# ---- end to end on SQLite ---------------------------------------------------

async def test_feedback_and_llm_text_are_embedded_and_searchable(tmp_path, monkeypatch):
    """The real middleware with an EmbeddingSink (fake embedder): a feedback
    event and a captured llm.call both land in ffb_embeddings, without image
    data, and similar_feedback finds the related LLM text."""
    from fastmcp_feedback.instrumentation import (
        DatabaseSink,
        EmbeddingSink,
        build_metadata,
        instrument,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
    metadata = build_metadata(prefix=instrumentation.FFB_TABLE_PREFIX,
                              embedding_dim=instrumentation.FFB_EMBEDDING_DIM)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    db = DatabaseSink(engine, prefix=instrumentation.FFB_TABLE_PREFIX,
                      embedding_dim=instrumentation.FFB_EMBEDDING_DIM)
    embedder = FakeEmbedder()
    app = FastMCP("t")
    app.qa_middleware = mw = instrument(app, [db, EmbeddingSink(embedder, db)], mode="meta",
                                        capture_llm_text=True)
    try:
        feedback_tools._record_submitted(SimpleNamespace(fastmcp=app), _row(), "user-1")
        monkeypatch.setattr(instrumentation, "middleware", lambda ctx=None: mw)
        handler = RoutingSamplingHandler(config_loader=_cfg,
                                         transport=_gateway("the render hangs in Cycles"))
        msgs = [SamplingMessage(role="user", content=[
            TextContent(type="text", text="why does my render hang?"), _image()])]
        await handler.complete(Backend("gateway", "qwen3", "https://gw.example/v1", "gw-secret"),
                               None, msgs, None)
        await mw.flush()
        await mw.embedding_sink.flush()

        table = metadata.tables[f"{instrumentation.FFB_TABLE_PREFIX}embeddings"]
        async with engine.connect() as conn:
            rows = (await conn.execute(select(table))).mappings().all()
        by_type = {r["source_type"]: r for r in rows}
        assert set(by_type) == {"feedback", "llm"}
        assert by_type["feedback"]["source_id"] == "bug-AbC123"
        assert by_type["feedback"]["text"] == "Render hangs\nCycles never finishes"
        assert "why does my render hang?" in by_type["llm"]["text"]
        assert "the render hangs in Cycles" in by_type["llm"]["text"]
        assert all(IMAGE_B64[:40] not in t for t in embedder.seen)
        assert all("gw-secret" not in t for t in embedder.seen)

        related = await mw.similar_feedback("bug-AbC123")
        assert [r["source_type"] for r in related] == ["llm"]
    finally:
        await mw.aclose()
        await engine.dispose()
