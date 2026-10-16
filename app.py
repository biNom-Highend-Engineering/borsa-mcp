"""
ASGI application for Borsa MCP Server

This is the production ASGI application that can be run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

The MCP server will be available at:
    http://localhost:8000/mcp/
"""

import os

from starlette.responses import JSONResponse
from health import check_dependencies, dependencies_for
from unified_mcp_server import app as mcp

# Add health check endpoint to the MCP server
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Dokploy and other monitoring services"""
    # Experiment: /health is already excluded from Easy Auth (Application
    # Insights can't do the OAuth handshake), so this is a second, app-level
    # gate layered on top of that exclusion -- a static shared secret rather
    # than a real credential, matching mevzuat-mcp/yargi-mcp's own trial.
    health_endpoint_secret = os.getenv("HEALTH_ENDPOINT_SECRET")
    if health_endpoint_secret and request.headers.get("X-Health-Check-Token") != health_endpoint_secret:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    requested_services = request.query_params.get("service")
    try:
        if not requested_services:
            # The default readiness check covers both the MCP tool surface and
            # every registered external service used by those tools.
            dependencies = None
        elif requested_services.strip().lower() == "all":
            dependencies = None
        else:
            dependencies = dependencies_for(requested_services.split(","))
    except ValueError as exc:
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=400,
        )

    body, status_code = await check_dependencies(
        dependencies=dependencies,
        mcp_app=mcp,
    )
    return JSONResponse(body, status_code=status_code)


@mcp.custom_route("/health/live", methods=["GET"])
async def liveness(request):
    """Liveness only - no upstream checks, so it never reports someone else's outage."""
    return JSONResponse({"status": "ok", "service": "Borsa MCP Server"})

# Create ASGI app directly from FastMCP server
# This avoids routing issues with nested mounts.
#
# stateless_http=True: every request is self-contained; no per-client transport
# object is kept server-side. The stateful default keeps a transport (task group,
# memory streams, buffers) per `initialize` until the client sends DELETE /mcp —
# which claude.ai and most clients never do. Measured on the live container:
# ~11.5k sessions opened / ~750 closed per day, ~1-1.3 GB/day growth, and two
# OOM-kills at 10.8 GB RSS (2026-08-27, 2026-09-07). Nothing here needs a session:
# no progress, sampling, subscriptions or elicitation — plain request/response tools.
app = mcp.http_app(stateless_http=True)

# Endpoints:
# - /mcp/ - MCP server (Streamable HTTP transport, default FastMCP path)
# - /health - MCP readiness plus every registered upstream check
# - /health?service=yahoo_finance,tcmb - readiness plus selected upstream checks
# - /health?service=all - readiness plus every registered upstream check
# - /health/live - liveness only, never touches an upstream
# Run with: uvicorn app:app --host 0.0.0.0 --port 8000
