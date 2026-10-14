# ADR-0006: MCP SDK 2.x over stateless streamable HTTP; A2A 0.3.0 JSON-RPC

- Status: Accepted
- Date: 2026-10-14

## Context

Agents should query operational state through the protocols they already
speak. The portfolio standard calls for the official MCP Python SDK and an
A2A Agent Card with `message/send`.

## Decision

- Use `mcp` 2.x and its `MCPServer` class, the successor of `FastMCP` from
  the 1.x SDK. Serve streamable HTTP at `/mcp` in stateless mode with JSON
  responses, so there is no session affinity and any replica answers any
  request.
- Tool parameters carry constraints and tools return Pydantic models, so the
  published tool list includes input and output schemas
  (`docs/api/mcp-tools.json`, checked for drift in CI).
- Authorize every tool call with the caller's bearer credential and the
  tool's own scope, and audit every call.
- Implement A2A protocol 0.3.0 with JSON-RPC transport: Agent Card at
  `/.well-known/agent-card.json`, `POST /a2a` handling `message/send`, skills
  selected by DataPart or by fixed text rules, replies as Tasks.

## Consequences

- MCP 2.x differs from 1.x examples found elsewhere; the SDK version is
  pinned through `uv.lock`.
- Stateless MCP gives up server-initiated notifications, which no tool needs.
- Streaming and push notifications are declared unsupported in the Agent Card.
