"""
ASGI application for Borsa MCP Server

This is the production ASGI application that can be run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

The MCP server will be available at:
    http://localhost:8000/mcp/
"""

from starlette.responses import JSONResponse
from health import check_dependencies, dependencies_for
from unified_mcp_server import app as mcp

# Add health check endpoint to the MCP server
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Dokploy and other monitoring services"""
    requested_services = request.query_params.get("service")
    try:
        dependencies = (
            dependencies_for(requested_services.split(","))
            if requested_services
            else None
        )
    except ValueError as exc:
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=400,
        )

    body, status_code = await check_dependencies(dependencies=dependencies)
    return JSONResponse(body, status_code=status_code)

# Create ASGI app directly from FastMCP server
# This avoids routing issues with nested mounts
app = mcp.http_app()

# Endpoints:
# - /mcp/ - MCP server (Streamable HTTP transport, default FastMCP path)
# - /health - Health check for monitoring
# Run with: uvicorn app:app --host 0.0.0.0 --port 8000
