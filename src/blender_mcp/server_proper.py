"""FastMCP server bootstrap.

Two builders, one per transport — because the bus tools rely on a per-request
`ContextVar` that the FastAPI middleware sets from a JWT. Over stdio there's
no way to propagate that identity, so registering the bus tools there just
gives every caller `unauthenticated` and pollutes tool discovery.

- `build_stdio_mcp()` → diagnostics + install only (open access)
- `build_http_mcp()`  → diagnostics + bus tools (the bus is OAuth-gated by
                        the FastAPI middleware in `oauth_server.py`)

`oauth_server` imports the module-level `mcp` (the HTTP build); the stdio
entry point in `main()` constructs the stdio build on demand.
"""

import logging

from fastmcp import FastMCP

from .bus_tools import BlenderBusComponent
from .diagnostics_component import BlenderDiagnosticsComponent
from .dispatch_component import BlenderDispatchComponent

logger = logging.getLogger(__name__)


def build_stdio_mcp() -> FastMCP:
    """Build the stdio MCP server.

    Diagnostics + install only. Bus tools are omitted because the stdio
    transport can't carry user identity — registering them would mean every
    call returns `unauthenticated`, which is worse than not seeing the tool
    at all (it pollutes tool listings and misleads clients).
    """
    server = FastMCP("BlenderMCP")
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    logger.info("FastMCP server built (stdio): diagnostics only")
    return server


def build_http_mcp() -> FastMCP:
    """Build the HTTP MCP server.

    Diagnostics + bus tools + dispatch tools. The bus tools and dispatch
    tools both resolve user identity from a ContextVar set by the FastAPI
    middleware in `oauth_server.py` after JWT verification.

    - Diagnostics: open-access helpers (Blender install probes)
    - Bus tools: low-level register/send/list (used by addon + advanced clients)
    - Dispatch tools: flat round-trip tools for the 24 addon commands;
      shield MCP clients from job_id correlation and notification-listening
    """
    server = FastMCP("BlenderMCP")
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    BlenderBusComponent().register_all(mcp_server=server, prefix="blender")
    BlenderDispatchComponent().register_all(mcp_server=server, prefix="blender")
    logger.info("FastMCP server built (HTTP): diagnostics + bus + dispatch")
    return server


# Module-level instance for `from .server_proper import mcp` (oauth_server).
mcp = build_http_mcp()


def main():
    """Standalone stdio entrypoint (for non-HTTP usage).

    Builds its own stdio-flavored server rather than reusing the module-level
    HTTP `mcp`, so stdio callers don't see bus tools they can never use.
    """
    try:
        from importlib.metadata import version
        v = version("blender-mcp")
    except Exception:
        v = "0.0.0"
    print(f"BlenderMCP v{v} — stdio mode (diagnostics only)")
    build_stdio_mcp().run()


if __name__ == "__main__":
    main()
