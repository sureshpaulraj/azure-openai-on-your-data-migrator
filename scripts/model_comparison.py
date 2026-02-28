"""Model comparison test — runs the same migration + queries across multiple models."""
import os, time, json, requests
from azure.identity import AzureCliCredential

EP = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
CONN = os.environ["SEARCH_CONNECTION_NAME"]
IDX = os.environ["SEARCH_INDEX_NAME"]
QT = os.environ.get("SEARCH_QUERY_TYPE", "semantic")
V = "2025-05-01"

MODELS = ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"]
QUERIES = [
    "What is the company leave policy?",
    "Summarize the data security guidelines.",
    "What are the employee benefits?",
]

cred = AzureCliCredential(process_timeout=30)
tok = cred.get_token("https://ai.azure.com/.default").token
h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def get_connection_id():
    r = requests.get(f"{EP}/connections?api-version={V}", headers=h)
    r.raise_for_status()
    for c in r.json().get("value", []):
        if c["name"] == CONN:
            return c["id"]
    raise ValueError(f"Connection '{CONN}' not found")


def create_agent(model, conn_id):
    body = {
        "name": f"model-cmp-{model}",
        "model": model,
        "instructions": "You are an HR assistant. Use the search tool to find HR policy information before answering questions.",
        "tools": [{"type": "azure_ai_search"}],
        "tool_resources": {
            "azure_ai_search": {
                "indexes": [{"index_connection_id": conn_id, "index_name": IDX, "query_type": QT, "top_k": 5}]
            }
        },
    }
    r = requests.post(f"{EP}/assistants?api-version={V}", json=body, headers=h)
    r.raise_for_status()
    return r.json()["id"]


def query_agent(agent_id, query):
    tid = requests.post(f"{EP}/threads?api-version={V}", json={}, headers=h).json()["id"]
    requests.post(f"{EP}/threads/{tid}/messages?api-version={V}", json={"role": "user", "content": query}, headers=h)
    rid = requests.post(f"{EP}/threads/{tid}/runs?api-version={V}", json={"assistant_id": agent_id}, headers=h).json()["id"]

    t0 = time.time()
    for _ in range(45):
        st = requests.get(f"{EP}/threads/{tid}/runs/{rid}?api-version={V}", headers=h).json().get("status", "")
        if st in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(2)
    elapsed = time.time() - t0

    resp, cites = "", 0
    if st == "completed":
        for m in requests.get(f"{EP}/threads/{tid}/messages?api-version={V}", headers=h).json().get("data", []):
            if m["role"] == "assistant":
                for c in m.get("content", []):
                    if c.get("type") == "text":
                        resp = c["text"]["value"]
                        cites = len(c["text"].get("annotations", []))
                        break
                break
    return {"status": st, "response": resp, "citations": cites, "latency_s": round(elapsed, 1), "response_len": len(resp)}


def delete_agent(agent_id):
    requests.delete(f"{EP}/assistants/{agent_id}?api-version={V}", headers=h)


conn_id = get_connection_id()
print(f"Connection: {CONN} ({conn_id[:60]}...)")
print(f"Index: {IDX} | Query type: {QT}")
print(f"Models: {MODELS}")
print(f"Queries: {len(QUERIES)}\n")
print("=" * 100)

results = {}
for model in MODELS:
    print(f"\n{'='*40} {model} {'='*40}")
    t0 = time.time()
    agent_id = create_agent(model, conn_id)
    create_time = round(time.time() - t0, 1)
    print(f"  Agent created in {create_time}s (ID: {agent_id})")

    model_results = []
    for q in QUERIES:
        r = query_agent(agent_id, q)
        preview = r["response"][:150].replace("\n", " ") if r["response"] else r["status"]
        print(f"  Q: {q}")
        print(f"    Status: {r['status']} | Latency: {r['latency_s']}s | Chars: {r['response_len']} | Citations: {r['citations']}")
        print(f"    Preview: {preview}...")
        model_results.append(r)

    results[model] = {
        "agent_id": agent_id,
        "create_time_s": create_time,
        "queries": model_results,
        "avg_latency_s": round(sum(r["latency_s"] for r in model_results) / len(model_results), 1),
        "avg_response_len": round(sum(r["response_len"] for r in model_results) / len(model_results)),
        "total_citations": sum(r["citations"] for r in model_results),
        "success_rate": sum(1 for r in model_results if r["status"] == "completed") / len(model_results) * 100,
    }

    delete_agent(agent_id)
    print(f"  Agent deleted.")

# Summary table
print("\n\n" + "=" * 100)
print(f"{'MODEL COMPARISON SUMMARY':^100}")
print("=" * 100)
print(f"{'Model':<16} {'Avg Latency':>12} {'Avg Resp Len':>14} {'Citations':>10} {'Success':>10} {'Create':>10}")
print("-" * 100)
for model, data in results.items():
    print(f"{model:<16} {data['avg_latency_s']:>10.1f}s {data['avg_response_len']:>12} ch {data['total_citations']:>9} {data['success_rate']:>9.0f}% {data['create_time_s']:>8.1f}s")
print("=" * 100)

# Recommendation
best = min(results.items(), key=lambda x: x[1]["avg_latency_s"])
richest = max(results.items(), key=lambda x: x[1]["avg_response_len"])
most_cited = max(results.items(), key=lambda x: x[1]["total_citations"])
print(f"\nFastest:          {best[0]} ({best[1]['avg_latency_s']}s avg)")
print(f"Most detailed:    {richest[0]} ({richest[1]['avg_response_len']} chars avg)")
print(f"Most citations:   {most_cited[0]} ({most_cited[1]['total_citations']} total)")
