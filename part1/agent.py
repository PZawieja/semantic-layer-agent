"""
Agents on your semantic layer — Part 1: the foundation.
Blog post: https://goldlayer.dev/writing/semantic-layer-agent-build.html

Three tools, one loop, ~80 lines. Run:
    python agent.py "What was our MRR last quarter?"
"""

import json
import os
import sys

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Step 1 — Semantic Layer connection
# ---------------------------------------------------------------------------

def sl_query(gql: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        os.environ["DBT_SL_URL"],
        headers={
            "Authorization": f"Bearer {os.environ['DBT_SL_TOKEN']}",
            "Content-Type": "application/json",
        },
        json={"query": gql, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data


# ---------------------------------------------------------------------------
# Step 2 — Tool 1: list_metrics
# ---------------------------------------------------------------------------

_LIST_METRICS_GQL = """
query ListMetrics($environmentId: BigInt!) {
    metrics(environmentId: $environmentId) {
        name
        description
        type
        dimensions {
            name
            description
        }
    }
}
"""


def list_metrics() -> list[dict]:
    result = sl_query(
        _LIST_METRICS_GQL,
        {"environmentId": int(os.environ["DBT_ENV_ID"])},
    )
    return result["data"]["metrics"]


# ---------------------------------------------------------------------------
# Step 3 — Tool 2: execute_metric_query
# ---------------------------------------------------------------------------

_QUERY_GQL = """
query RunMetric(
    $environmentId: BigInt!
    $metrics: [MetricInput!]!
    $groupBy: [GroupByInput!]
    $where: [WhereInput!]
    $limit: Int
) {
    query(
        environmentId: $environmentId
        metrics: $metrics
        groupBy: $groupBy
        where: $where
        limit: $limit
    ) {
        sql
        jsonResult
    }
}
"""


def execute_metric_query(
    metric_name: str,
    dimensions: list[str],
    filters: list[str] | None = None,
    limit: int = 100,
) -> dict:
    variables: dict = {
        "environmentId": int(os.environ["DBT_ENV_ID"]),
        "metrics": [{"name": metric_name}],
        "groupBy": [{"name": d} for d in dimensions],
        "limit": limit,
    }
    if filters:
        variables["where"] = [{"sql": f} for f in filters]
    result = sl_query(_QUERY_GQL, variables)
    return result["data"]["query"]


# ---------------------------------------------------------------------------
# Step 4 — Tool 3: clarify_or_decline
# ---------------------------------------------------------------------------

def clarify_or_decline(
    reason: str,
    clarifying_question: str | None = None,
) -> dict:
    return {
        "status": "needs_clarification",
        "reason": reason,
        "question": clarifying_question,
    }


# ---------------------------------------------------------------------------
# Step 5 — Tool definitions (what Claude reads to decide when to call each)
# ---------------------------------------------------------------------------

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "list_metrics",
        "description": (
            "List all available metrics from the dbt Semantic Layer. "
            "Call this at the start of every session, before any query. "
            "Never guess at metric names — always check what exists first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "execute_metric_query",
        "description": (
            "Run a metric query against the dbt Semantic Layer. "
            "Before calling this, state your interpretation to the user: "
            "'I'm going to return [metric] for [period], grouped by [dimension]. Is that right?' "
            "Only proceed after they confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_name": {
                    "type": "string",
                    "description": "Exact metric name from list_metrics — do not invent names",
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dimension names to group by, from the metric's dimension list",
                },
                "filters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "SQL WHERE expressions, e.g. [\"metric_time__quarter = 'Q1-2026'\"]",
                },
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["metric_name", "dimensions"],
        },
    },
    {
        "name": "clarify_or_decline",
        "description": (
            "Use this when you cannot map the user's question to a metric with confidence, "
            "or when the question contains an ambiguous term you haven't resolved. "
            "This is the correct response when uncertain — do NOT guess at metric names "
            "or business logic definitions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you can't answer with confidence",
                },
                "clarifying_question": {
                    "type": "string",
                    "description": "The specific question to ask the user",
                },
            },
            "required": ["reason"],
        },
    },
]


# ---------------------------------------------------------------------------
# Step 6 — System prompt
# ---------------------------------------------------------------------------

ORG_DEFINITIONS = """
Company metric definitions — apply these unless the user explicitly overrides:
- churn: notification_date attribution, MRR basis
- active_customer: subscription_status = 'active' AND last_payment within 90 days
- MRR: sum of monthly_value for active subscriptions, excluding trials
"""

SYSTEM = f"""You are an analytics assistant connected to our dbt Semantic Layer on Snowflake.

Rules — non-negotiable:
1. Call list_metrics at the start of every session, before any other action.
2. Before running execute_metric_query, tell the user your exact interpretation:
   "I'm going to return [metric] for [period], grouped by [dimension]. Is that right?"
   Only execute after they confirm.
3. If you cannot map a question to a metric with confidence, use clarify_or_decline.
   A polite request for clarification is always better than a confident wrong answer.

{ORG_DEFINITIONS}"""


# ---------------------------------------------------------------------------
# Step 7 — The agent loop
# ---------------------------------------------------------------------------

def dispatch(name: str, inputs: dict) -> str:
    if name == "list_metrics":
        return json.dumps(list_metrics())
    if name == "execute_metric_query":
        return json.dumps(execute_metric_query(**inputs))
    if name == "clarify_or_decline":
        return json.dumps(clarify_or_decline(**inputs))
    return json.dumps({"error": f"unknown tool: {name}"})


def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": dispatch(block.name, block.input),
                }
                for block in response.content
                if block.type == "tool_use"
            ]
            messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    print(run_agent(" ".join(sys.argv[1:])))
