"""
Agents on your semantic layer — Part 2: hardening it.
Blog post: https://goldlayer.dev/writing/semantic-layer-agent-harden.html

Adds disambiguation pre-flight, template matching, and three-tier memory
on top of the Part 1 foundation. Run:
    python agent.py "What was our MRR last quarter?"
"""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Semantic Layer connection (unchanged from Part 1)
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


def list_metrics() -> list[dict]:
    result = sl_query(
        _LIST_METRICS_GQL,
        {"environmentId": int(os.environ["DBT_ENV_ID"])},
    )
    return result["data"]["metrics"]


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
# Tool definitions (unchanged from Part 1)
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
# Step 5 — Disambiguation pre-flight
# ---------------------------------------------------------------------------

DISAMBIGUATION: dict[str, dict] = {
    "churn": {
        "question": (
            "When you say churn — do you mean attribution to the contract end date "
            "or the cancellation notification date? And customer count or MRR?"
        ),
    },
    "revenue": {
        "question": "Do you need MRR, ARR, or recognized revenue?",
    },
    "active": {
        "question": "Active by subscription status, last login, or last payment date?",
    },
}


def check_disambiguation(question: str, resolved: set[str]) -> str | None:
    q = question.lower()
    for term, cfg in DISAMBIGUATION.items():
        if term in q and term not in resolved:
            return cfg["question"]
    return None


# ---------------------------------------------------------------------------
# Step 6 — Template library
# ---------------------------------------------------------------------------

@dataclass
class Template:
    name: str
    pattern: str
    build: Callable[[dict], dict]


TEMPLATES: list[Template] = [
    Template(
        name="period_metric",
        pattern=r"what was (?P<metric>.+?) (?:in |for |)(?P<period>last \w+|q[1-4] \d{4}|this \w+)",
        build=lambda m: {
            "metric_name": m["metric"].strip(),
            "dimensions": ["metric_time"],
            "filters": [f"metric_time__quarter = '{m['period'].upper()}'"],
        },
    ),
    Template(
        name="top_n",
        pattern=r"which (?P<dim>\w+) had the (?:highest|best|most) (?P<metric>.+)",
        build=lambda m: {
            "metric_name": m["metric"].strip(),
            "dimensions": [m["dim"]],
            "limit": 10,
        },
    ),
    Template(
        name="yoy",
        pattern=r"(?P<metric>.+?) (?:vs\.?|versus|compared to) (?:same period |)last year",
        build=lambda m: {
            "metric_name": m["metric"].strip(),
            "dimensions": ["metric_time"],
            "filters": [
                "metric_time__year IN (YEAR(CURRENT_DATE), YEAR(CURRENT_DATE) - 1)"
            ],
        },
    ),
]


def match_template(question: str) -> tuple[str | None, dict | None]:
    for tmpl in TEMPLATES:
        if m := re.search(tmpl.pattern, question.lower()):
            return tmpl.name, tmpl.build(m.groupdict())
    return None, None


# ---------------------------------------------------------------------------
# Step 7 — Three-tier memory
# ---------------------------------------------------------------------------

class SessionMemory:
    def __init__(self, user_id: str):
        self._session: dict[str, str] = {}
        self._path = Path(f".memory/{user_id}.json")
        self._persistent: dict[str, str] = (
            json.loads(self._path.read_text()) if self._path.exists() else {}
        )

    def get(self, key: str) -> str | None:
        return self._session.get(key) or self._persistent.get(key)

    def set(self, key: str, value: str, persist: bool = False) -> None:
        self._session[key] = value
        if persist:
            self._persistent[key] = value
            self._path.parent.mkdir(exist_ok=True)
            self._path.write_text(json.dumps(self._persistent, indent=2))

    def resolved_terms(self) -> set[str]:
        return set(self._session) | set(self._persistent)

    def context_block(self) -> str:
        if not self._persistent:
            return ""
        prefs = "\n".join(f"- {k}: {v}" for k, v in self._persistent.items())
        return f"User preferences (apply unless overridden this session):\n{prefs}"


# ---------------------------------------------------------------------------
# Step 8 — The hardened agent loop
# ---------------------------------------------------------------------------

def dispatch(name: str, inputs: dict) -> str:
    if name == "list_metrics":
        return json.dumps(list_metrics())
    if name == "execute_metric_query":
        return json.dumps(execute_metric_query(**inputs))
    if name == "clarify_or_decline":
        return json.dumps(clarify_or_decline(**inputs))
    return json.dumps({"error": f"unknown tool: {name}"})


def run_agent(question: str, user_id: str = "default") -> str:
    memory = SessionMemory(user_id)

    # Pre-flight 1: disambiguation
    clarification = check_disambiguation(question, memory.resolved_terms())
    if clarification:
        return f"Before I pull that number — {clarification}"

    # Pre-flight 2: template match
    tmpl_name, tmpl_args = match_template(question)
    if tmpl_name and tmpl_args:
        result = execute_metric_query(**tmpl_args)
        return f"[template: {tmpl_name}]\n{json.dumps(result, indent=2)}"

    # Full agent loop for everything else
    system = f"{SYSTEM}\n\n{memory.context_block()}"
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "clarify_or_decline":
                    cq = block.input.get("clarifying_question")
                    return cq if cq else block.input.get("reason", "I need more context.")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": dispatch(block.name, block.input),
                })
            messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    print(run_agent(" ".join(sys.argv[1:])))
