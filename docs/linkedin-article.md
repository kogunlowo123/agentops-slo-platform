# Error budgets for AI agents

An agent can return a polite answer while a tool failed three times underneath and a model call was throttled. Our dashboards called that a success.

**Problem.** Agent platforms emit traces, yet few teams turn them into service level objectives, cost ownership or safe remediation. Spend follows tokens, not CPU, and lands on whoever hosts the agent.

**Why it matters.** Without error budgets, paging is either noisy or late. Without attribution, nobody owns the bill.

**Architecture.** Task → Trace → Metrics → Cost → Recovery. Agents emit OpenTelemetry spans using the GenAI conventions. A collector groups them by trace and strips prompt content. We classify each span as agent, tool, model, memory or workflow, attribute it to the nearest agent and team, and price it with the rate in force at that moment. SLOs use multi-window, multi-burn-rate rules from the Google SRE Workbook. Each trace becomes a classified run with a root cause. Agents query all of it over MCP and A2A, authorized by our identity fabric.

**Implementation.** OTLP/HTTP ingestion keyed by trace and span ID, so retries never double count. Burn-rate factors derived from budget share, not hard-coded. Retry policies derived from how often retries actually succeed. Circuit state from short and long windows.

**Practices.** No model in the control loop. Risky actions such as opening a circuit wait for a second engineer, then become expiring directives that runtimes pull. Every decision is signed into an audit chain. Metric labels from telemetry are capped.

**Tradeoffs.** Storing span facts gives exact SLIs but grows with volume, so partitioning and rollups come next. Low-traffic SLOs may never alert. Prices are configuration, and keeping them current is real work.

**Value.** Teams see which agent burned the budget, what it cost and what to change, with evidence an auditor can follow.

**Looking ahead.** As agents take on more autonomous work, reliability engineering has to measure them in their own units.

Should an agent be allowed to open a circuit on a tool without a human approving it?

https://github.com/kogunlowo123/agentops-slo-platform

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #OpenTelemetry #SRE #FinOps
