"""Export published contracts: JSON Schemas, MCP tool definitions and the OpenAPI document.

python -m agentops_slo_platform.contracts            # rewrite schemas/ and docs/api/
python -m agentops_slo_platform.contracts --check    # exit 1 if committed files are stale
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from pydantic import BaseModel

from agentops_slo_platform.api.app import create_app
from agentops_slo_platform.application.analytics import OperationReport, WorkflowView
from agentops_slo_platform.domain.costs import CostReport, PriceEntry
from agentops_slo_platform.domain.recovery import (
    ProposeAction,
    RecoveryAction,
    RecoveryRecommendation,
)
from agentops_slo_platform.domain.slo import SloDefinition, SloStatus
from agentops_slo_platform.domain.spans import SpanFact
from agentops_slo_platform.integrations.mcp_server import build_mcp_server

MODELS: dict[str, type[BaseModel]] = {
    "slo-definition": SloDefinition,
    "slo-status.response": SloStatus,
    "price-entry": PriceEntry,
    "cost-report.response": CostReport,
    "operation-report.response": OperationReport,
    "workflow.response": WorkflowView,
    "recovery-recommendation.response": RecoveryRecommendation,
    "propose-action.request": ProposeAction,
    "recovery-action.response": RecoveryAction,
    "span-fact": SpanFact,
}


async def _no_container() -> Any:  # pragma: no cover - never started
    raise RuntimeError("contract export does not start the application")


def mcp_tools() -> dict[str, Any]:
    """tools/list as served by the MCP endpoint (dependencies are never invoked)."""
    server = build_mcp_server(
        guard=cast(Any, None),
        audit=cast(Any, None),
        slos=cast(Any, None),
        analytics=cast(Any, None),
        recovery=cast(Any, None),
    )
    tools = asyncio.run(server.list_tools())
    return {
        "tools": [
            t.model_dump(mode="json", by_alias=True, exclude_none=True)
            for t in sorted(tools, key=lambda t: t.name)
        ]
    }


def documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        f"schemas/{name}.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://github.com/kogunlowo123/agentops-slo-platform/"
                f"schemas/{name}.schema.json"
            ),
            **model.model_json_schema(mode="validation"),
        }
        for name, model in MODELS.items()
    }
    app: FastAPI = create_app(_no_container)
    docs["docs/api/openapi.json"] = app.openapi()
    docs["docs/api/mcp-tools.json"] = mcp_tools()
    return docs


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = root or Path.cwd()
    stale: list[str] = []
    for relative, document in documents().items():
        path = base / relative
        content = render(document)
        if "--check" in args:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(
            "stale contracts (run python -m agentops_slo_platform.contracts): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
