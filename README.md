# databricks-enterprise-ai-patterns
Production-grade AI and ML patterns for the Databricks platform, built from real enterprise consulting work across Healthcare, Finance, Retail, and Manufacturing.

## Quick Start

```bash
# Start a pattern interactively
./demo.sh start lakeflow-job
./demo.sh start background-task --profile myprofile --smoke

# Stop or destroy
./demo.sh stop lakeflow-job --profile myprofile --destroy

# Run smoke tests
./demo.sh smoke background-task
```

## Patterns

### Async Inference

| Pattern | Description |
|---|---|
| [`async-inference/lakeflow-job`](async-inference/lakeflow-job/) | Async LLM inference via Lakeflow serverless worker jobs + reconciler. Best for high-volume, independently scalable workloads. |
| [`async-inference/background-task`](async-inference/background-task/) | Async LLM inference via in-process asyncio background tasks. Simpler infrastructure (no worker job, no reconciler), near-zero trigger latency. |

## Acknowledgements

The async inference patterns were inspired by the Databricks App Templates [OpenAI Agents SDK Long-Running Agent](https://github.com/databricks/app-templates/tree/main/agent-openai-agents-sdk-long-running-agent).
