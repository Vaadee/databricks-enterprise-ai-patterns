# databricks-enterprise-ai-patterns
Production-grade AI and ML patterns for the Databricks platform, built from real enterprise consulting work across Healthcare, Finance, Retail, and Manufacturing.

## Patterns

### Async Inference

| Pattern | Description |
|---|---|
| [`async-inference/long-running-inference`](async-inference/long-running-inference/) | Async LLM inference via Lakeflow serverless worker jobs + reconciler. Best for high-volume, independently scalable workloads. |
| [`async-inference/background-async-inference`](async-inference/background-async-inference/) | Async LLM inference via in-process asyncio background tasks. Simpler infrastructure (no worker job, no reconciler), near-zero trigger latency. |
