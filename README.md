# Agents on your semantic layer

Companion code for the three-part series on building a production-safe analytics agent on top of the dbt Semantic Layer.

| Part | Post | What it covers |
|------|------|----------------|
| Part 0 | [Your analytics agent is confident. That's the problem.](https://goldlayer.dev/writing/agents-on-semantic-layer-101.html) | Why semantic layer agents fail — hallucinated metric names, wrong dimension grain, silent wrong answers. No code, pure concepts. |
| Part 1 | [Three tools. One loop. A working analytics agent.](https://goldlayer.dev/writing/semantic-layer-agent-build.html) | Build the foundation: three tools, one Claude loop, ~80 lines of Python. |
| Part 2 | [Three additions that make it production-safe.](https://goldlayer.dev/writing/semantic-layer-agent-harden.html) | Add disambiguation pre-flight, template matching, and three-tier memory. |

---

## Setup

```bash
uv init analytics-agent
cd analytics-agent
uv add anthropic requests python-dotenv
```

Or install from this repo's `pyproject.toml`:

```bash
uv sync
```

Copy the environment template and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Where to find it |
|----------|-----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `DBT_SL_URL` | Your dbt Cloud Semantic Layer endpoint (shown in project settings) |
| `DBT_SL_TOKEN` | A dbt Cloud service token with **Semantic Layer Only** permissions |
| `DBT_ENV_ID` | The numeric ID from your dbt Cloud environment settings |

---

## Part 1 — the foundation

`part1/agent.py` implements the three-tool architecture from scratch:

- `list_metrics` — orientation call at session start; the agent learns what exists before any query
- `execute_metric_query` — runs a named metric query against the Semantic Layer
- `clarify_or_decline` — gives the agent a typed exit when it can't answer with confidence

```bash
cd part1
python agent.py "What was our MRR last quarter?"
```

The agent will:
1. Call `list_metrics` to load the catalog
2. State its interpretation ("I'm going to return monthly_recurring_revenue for Q1 2026…")
3. Wait for your confirmation before running the query

Test it with 3–5 of your most common analytics questions before moving to Part 2. Pay attention to what happens when you ask "what's our churn rate?" — it will answer, but it won't ask which churn definition you mean.

---

## Part 2 — hardening it

`part2/agent.py` adds three layers on top of the Part 1 foundation:

**Disambiguation pre-flight** — checks the question for known ambiguous terms before the LLM sees it. "Churn" triggers a clarifying question. No API call, no latency.

**Template library** — regex patterns for common question types. "What was MRR last quarter?" matches `period_metric` and resolves without any LLM reasoning — instant, deterministic, no token cost.

**Three-tier memory** — session dict (cleared on exit), user JSON under `.memory/` (persists across sessions), and `ORG_DEFINITIONS` in the system prompt (company-wide, always applied). Once a user answers "contract end or notification date?", they're never asked again.

```bash
cd part2
python agent.py "What was our MRR last quarter?"
```

### Five tests to run before your first real deployment

1. Ask `"What's our churn rate?"` cold → should fire the disambiguation question, not return a number
2. Answer it, then ask again with the same `user_id` → should skip the question and apply the saved preference
3. Ask for a metric name that doesn't exist → should call `list_metrics`, map to a real name, or use `clarify_or_decline`
4. Ask `"How does MRR compare to last year?"` → should match the `yoy` template instantly, no LLM reasoning
5. Ask about something your Semantic Layer doesn't have → should use `clarify_or_decline`, not invent a metric

---

## Customising for your org

**`ORG_DEFINITIONS`** (in both `part1/agent.py` and `part2/agent.py`) — replace the example definitions with your company's agreed definitions before any real deployment. What counts as "churn"? What's an "active customer"? Getting alignment on these is the hard part.

**`DISAMBIGUATION`** (in `part2/agent.py`) — start with 3–5 terms that your team actually disagrees on. Add more only when a real question slips through the wrong answer bucket.

**`TEMPLATES`** (in `part2/agent.py`) — the three starter templates cover period lookups, top-N rankings, and year-over-year comparisons. Add templates for your team's Monday morning questions.

**`.memory/`** — per-user JSON files under this directory. Replace with a DB-backed store if you're building a multi-tenant deployment; the `get` / `set` / `context_block` interface stays the same.

---

## Async query pattern

The dbt Cloud Semantic Layer may return a `queryId` instead of inline `jsonResult` for large result sets. If you see a `queryId` in the response, add a polling loop:

```python
def poll_query(query_id: str) -> dict:
    poll_gql = """
    query FetchResults($queryId: String!) {
        fetchResults(queryId: $queryId) {
            status
            jsonResult
        }
    }
    """
    while True:
        result = sl_query(poll_gql, {"queryId": query_id})
        data = result["data"]["fetchResults"]
        if data["status"] == "SUCCESSFUL":
            return data
        time.sleep(1)
```

Check the [dbt Semantic Layer GraphQL docs](https://docs.getdbt.com/docs/dbt-cloud-apis/sl-graphql) for the current schema.

## Series

This repo is project 3 of a series on AI + analytics engineering:

| # | Repo | What it adds |
|---|---|---|
| 1 | [dbt-agentic-engine](https://github.com/PZawieja/dbt-agentic-engine) | Agentic tool-use + structured trace |
| 2 | [llmops-eval-harness](https://github.com/PZawieja/llmops-eval-harness) | Scored eval loop, cost/latency tracking, regression gate |
| 3 | [semantic-layer-agent](https://github.com/PZawieja/semantic-layer-agent) (this repo) | Agent on the dbt Semantic Layer: disambiguation, templates, memory |
| 4 | [governed-analytics-mcp](https://github.com/PZawieja/governed-analytics-mcp) | MCP server that queries as the asker: warehouse RLS, persona evals, audit log |
| 5 | [dbt-llm-enrich](https://github.com/PZawieja/dbt-llm-enrich) | LLM classification as a tested dbt layer: content-hash cache, Batches API, golden-set accuracy tests, cost table |
| 6 | [observability-on-a-budget](https://github.com/PZawieja/observability-on-a-budget) | SQL monitors in dbt, rules triage, advisory AI notes, all scored against planted faults |

## License

MIT
