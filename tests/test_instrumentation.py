"""blender-mcp's fastmcp-feedback instrumentation wiring (QA_LOG)."""

import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from blender_mcp import instrumentation

FAKE_PAT = "bmcp_" + "A" * 43
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJlc2lnbmF0dXJl"


@pytest.fixture
def identity(monkeypatch):
    """Stand in for the real auth lookups, which need a live OAuth request."""
    from blender_mcp import bus_tools, client_role

    monkeypatch.setattr(bus_tools, "_resolve_user_id", lambda ctx: "user-sub-1")
    monkeypatch.setattr(client_role, "get_caller_role", lambda ctx=None: "llm-client")


# ---- enricher ---------------------------------------------------------------

def test_enrich_takes_ids_from_args_and_result_head():
    result = [json.dumps({"status": "running", "job_id": "j-0123456789ab",
                          "target_uuid": "blender-abc"})]
    out = instrumentation.enrich("blender_execute_code", {"bus_id": "b" * 8}, result, None)
    assert out["job_id"] == "j-0123456789ab"
    assert out["target_uuid"] == "blender-abc"
    assert out["bus_id"] == "b" * 8


def test_enrich_only_scans_the_head_of_huge_results():
    huge = "x" * 5_000_000 + '"job_id": "j-ffffffffffff"'
    out = instrumentation.enrich("blender_execute_code", {}, [huge], None)
    assert "job_id" not in out


def test_enrich_reports_server_version(monkeypatch):
    monkeypatch.setattr(instrumentation, "SERVER_VERSION", "2026.927.1")
    assert instrumentation.enrich("t", {}, None, None)["server_version"] == "2026.927.1"


# ---- identity ---------------------------------------------------------------

def test_identify_never_raises_without_a_request():
    assert isinstance(instrumentation.identify(SimpleNamespace(fastmcp_context=None)), dict)


def test_identify_uses_existing_auth_helpers(identity):
    out = instrumentation.identify(SimpleNamespace(fastmcp_context=None))
    assert out["user_sub"] == "user-sub-1"
    assert out["caller_kind"] == "llm-client"


# ---- switch -----------------------------------------------------------------

def test_qa_log_unset_installs_nothing(monkeypatch):
    monkeypatch.delenv("QA_LOG", raising=False)
    app = FastMCP("t")
    before = list(app.middleware)
    assert instrumentation.install(app) is None
    assert list(app.middleware) == before


def test_unknown_qa_log_value_is_off(monkeypatch):
    monkeypatch.setenv("QA_LOG", "1")
    assert instrumentation.qa_mode() == "off"


# ---- end to end: redaction and columns --------------------------------------

async def test_records_land_redacted_with_identity(tmp_path, monkeypatch, identity):
    from fastmcp_feedback.instrumentation import (
        DatabaseSink,
        build_metadata,
        instrument,
    )

    monkeypatch.setattr(instrumentation, "SERVER_VERSION", "2026.927.1")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
    metadata = build_metadata(prefix=instrumentation.FFB_TABLE_PREFIX)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    app = FastMCP("t")
    mw = instrument(
        app,
        [DatabaseSink(engine, prefix=instrumentation.FFB_TABLE_PREFIX)],
        mode="full",
        meta_only_tools=instrumentation.META_ONLY_TOOLS,
        identity_resolver=instrumentation.identify,
        enricher=instrumentation.enrich,
        server_version=instrumentation.SERVER_VERSION,
    )

    @app.tool
    def mint(note: str) -> str:
        return json.dumps({"token": FAKE_PAT, "echo": f"id {FAKE_JWT}", "job_id": "j-0123456789ab"})

    async with Client(app) as c:
        r = await c.call_tool("mint", {"note": f"arg carries {FAKE_PAT}"})
    text = r.content[0].text
    assert FAKE_PAT in text and FAKE_JWT in text  # caller gets the real values

    await mw.flush()
    table = metadata.tables[f"{instrumentation.FFB_TABLE_PREFIX}tool_calls"]
    async with engine.connect() as conn:
        row = (await conn.execute(select(table))).mappings().one()
    stored = json.dumps({"args": row["args"], "result": row["result"]}, default=str)
    assert FAKE_PAT not in stored and FAKE_JWT not in stored
    assert row["user_sub"] == "user-sub-1"
    assert row["caller_kind"] == "llm-client"
    assert row["server_version"] == "2026.927.1"
    assert row["tool"] == "mint" and row["ok"] is True and row["mode"] == "full"
    await mw.aclose()
    await engine.dispose()


async def test_meta_only_tools_never_store_payloads(tmp_path):
    from fastmcp_feedback.instrumentation import (
        DatabaseSink,
        build_metadata,
        instrument,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
    metadata = build_metadata(prefix=instrumentation.FFB_TABLE_PREFIX)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    app = FastMCP("t")
    mw = instrument(app, [DatabaseSink(engine, prefix=instrumentation.FFB_TABLE_PREFIX)],
                    mode="full", meta_only_tools=instrumentation.META_ONLY_TOOLS)

    @app.tool
    def blender_execute_code(code: str) -> str:
        return "ran"

    async with Client(app) as c:
        await c.call_tool("blender_execute_code", {"code": "print('secret scene data')"})
    await mw.flush()
    table = metadata.tables[f"{instrumentation.FFB_TABLE_PREFIX}tool_calls"]
    async with engine.connect() as conn:
        row = (await conn.execute(select(table))).mappings().one()
    assert row["mode"] == "meta" and row["args"] is None and row["result"] is None
    assert row["args_size"] > 0
    await mw.aclose()
    await engine.dispose()


# ---- feedback correlation ---------------------------------------------------

async def test_feedback_links_the_calls_that_led_up_to_it(tmp_path, identity):
    from fastmcp import Context
    from fastmcp_feedback.instrumentation import (
        DatabaseSink,
        build_metadata,
        instrument,
    )

    from blender_mcp.feedback_tools import _link_recent_calls

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
    metadata = build_metadata(prefix=instrumentation.FFB_TABLE_PREFIX)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    app = FastMCP("t")
    app.qa_middleware = instrument(
        app,
        [DatabaseSink(engine, prefix=instrumentation.FFB_TABLE_PREFIX)],
        mode="meta",
        identity_resolver=instrumentation.identify,
    )

    @app.tool
    def boom() -> str:
        raise RuntimeError("render failed")

    @app.tool
    async def report(ctx: Context) -> str:
        return str(await _link_recent_calls(ctx, "bug-test"))

    async with Client(app) as c:
        await c.call_tool("boom", {}, raise_on_error=False)
        r = await c.call_tool("report", {})
    assert r.content[0].text == "1"

    await app.qa_middleware.flush()
    calls = await app.qa_middleware.feedback_context("bug-test")
    assert [(x["tool"], x["ok"], x["error_type"]) for x in calls] == [("boom", False, "RuntimeError")]
    await app.qa_middleware.aclose()
    await engine.dispose()


async def test_feedback_linking_is_skipped_when_qa_log_is_off():
    from blender_mcp.feedback_tools import _link_recent_calls

    ctx = SimpleNamespace(fastmcp=SimpleNamespace(qa_middleware=None))
    assert await _link_recent_calls(ctx, "bug-x") is None
    assert await _link_recent_calls(None, "bug-x") is None
