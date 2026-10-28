"""Shared plumbing for the evaluation steps: config, Langfuse API, exported data, replay.

Replay keeps every exported id (traces, observations, dataset items, scores), so
objects created in different steps link up exactly as they did in the study.

Time: with DEMO_TIME_SHIFT=now, `load-traces` shifts every timestamp so the
newest investigation in the export lands at load time. Later steps read the
shift back from the first replayed trace, so all steps agree on it.
"""

import base64
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

REQUIRED_ENV = [
    "LANGFUSE_HOST",
    "LANGFUSE_UI_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "DEMO_TIME_SHIFT",
    "DEMO_DATA_DIR",
]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    sys.exit(f"Missing required env vars: {', '.join(missing)} (copy .env.example to .env)")
if "OPENROUTER_API_KEY" not in os.environ:
    sys.exit("OPENROUTER_API_KEY must be present in .env (empty is fine: the LLM steps then import saved results)")
if os.environ["DEMO_TIME_SHIFT"] not in ("now", "original"):
    sys.exit("DEMO_TIME_SHIFT must be 'now' or 'original'")

HOST = os.environ["LANGFUSE_HOST"].rstrip("/")
UI_URL = os.environ["LANGFUSE_UI_URL"].rstrip("/")
AUTH = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
DATA = Path(os.environ["DEMO_DATA_DIR"])
LLM_KEY = os.environ["OPENROUTER_API_KEY"].strip()

api = httpx.Client(base_url=f"{HOST}/api/public", auth=AUTH, timeout=120)

# The investigations each step replays, by the run (session) they belong to.
RUNS_LOADED_BY = {
    "load-traces": ["holmes-noise-run-1", "holmes-noise-run-2-steps100"],
}
# Trace whose replayed timestamp fixes the time shift for every later step.
ANCHOR_RUN = "holmes-noise-run-1"
# Metadata Langfuse derived from OTel spans; the OTLP replay recreates it.
OTEL_METADATA = ("attributes", "resourceAttributes", "scope")


# ---------------------------------------------------------------- output

def heading(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def say(text: str = "") -> None:
    print(text, flush=True)


@cache
def project_id() -> str:
    return check(api.get("/projects"), "Look up the project")["data"][0]["id"]


def ui(path: str) -> str:
    return f"{UI_URL}/project/{project_id()}/{path.lstrip('/')}"


# ---------------------------------------------------------------- API

def check(response: httpx.Response, what: str) -> dict:
    if response.is_error:
        sys.exit(f"{what} failed: HTTP {response.status_code}\n{response.text[:1000]}")
    return response.json() if response.content else {}


def paged(path: str, params: dict | None = None, limit: int = 100) -> list[dict]:
    rows, page = [], 1
    while True:
        body = check(api.get(path, params={**(params or {}), "page": page, "limit": limit}), f"GET {path}")
        rows += body["data"]
        if page >= body.get("meta", {}).get("totalPages", 1):
            return rows
        page += 1


def exists(path: str) -> bool:
    response = api.get(path)
    if response.status_code == 404:
        return False
    check(response, f"GET {path}")
    return True


def ingest(events: list[dict]) -> None:
    """Send ingestion events in small batches; any rejected event stops the step."""
    now = datetime.now(timezone.utc).isoformat()
    for start in range(0, len(events), 20):
        batch = [{"timestamp": now, **e} for e in events[start:start + 20]]
        body = check(api.post("/ingestion", json={"batch": batch}), "Ingestion")
        if body.get("errors"):
            sys.exit(f"Ingestion rejected {len(body['errors'])} event(s): {json.dumps(body['errors'])[:1000]}")


def require(condition: bool, step: str, what: str) -> None:
    if not condition:
        sys.exit(f"{what} is not in Langfuse yet: run `make {step}` first.")


# ---------------------------------------------------------------- exported data

@cache
def load(name: str):
    path = DATA / name
    if name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text())


def traces_of_runs(runs: list[str]) -> list[dict]:
    """Exported investigation traces (trace + observations) from these runs."""
    return [t for t in load("traces.json.gz") if t["trace"].get("sessionId") in runs]


def replay_traces() -> list[dict]:
    """The evaluator-replay traces the SDK experiment created."""
    return [t for t in load("traces.json.gz") if t["trace"]["environment"] == "sdk-experiment"]


def scores_named(names: set[str], trace_ids: set[str]) -> list[dict]:
    return [s for s in load("scores.json.gz") if s["name"] in names and s["traceId"] in trace_ids]


def dataset(name: str) -> dict:
    return next(d for d in load("datasets.json") if d["name"] == name)


def dataset_run(name: str) -> dict:
    return next(r for r in load("dataset_runs.json") if r["name"] == name)


# ---------------------------------------------------------------- time

def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def anchor() -> dict:
    return min(traces_of_runs([ANCHOR_RUN]), key=lambda t: t["trace"]["timestamp"])["trace"]


def new_shift() -> timedelta:
    """The shift `load-traces` applies: newest exported investigation -> now."""
    if os.environ["DEMO_TIME_SHIFT"] == "original":
        return timedelta(0)
    newest = max(parse(t["trace"]["timestamp"]) for t in load("traces.json.gz"))
    return datetime.now(timezone.utc).replace(microsecond=0) - newest


@cache
def shift() -> timedelta:
    """The shift `load-traces` applied, read back from the replayed anchor trace."""
    original = anchor()
    response = api.get(f"/traces/{original['id']}")
    require(response.status_code != 404, "load-traces", "The investigation traces")
    loaded = check(response, "Read the anchor trace")
    return parse(loaded["timestamp"]) - parse(original["timestamp"])


def shifted(ts: str | None, by: timedelta) -> str | None:
    return (parse(ts) + by).isoformat().replace("+00:00", "Z") if ts else None


# ---------------------------------------------------------------- OTLP replay

# Langfuse stores OTel attribute values as strings; these were numbers when sent.
NUMERIC_ATTRIBUTES = re.compile(r"^(gen_ai\.usage\..+|holmesgpt\.iteration|return_code|original_token_count|http\.status_code)$")


def any_value(value) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, list):
        return AnyValue(array_value=ArrayValue(values=[any_value(v) for v in value]))
    if isinstance(value, str):
        return AnyValue(string_value=value)
    return AnyValue(string_value=json.dumps(value))


def key_values(attributes: dict) -> list[KeyValue]:
    out = []
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str) and NUMERIC_ATTRIBUTES.match(key) and re.fullmatch(r"-?\d+", value):
            value = int(value)
        out.append(KeyValue(key=key, value=any_value(value)))
    return out


def ns(ts: str) -> int:
    return int(parse(ts).timestamp() * 1_000_000_000)


def as_attribute_text(value) -> str:
    # HolmesGPT sends input/output as text: JSON for structured values.
    return value if isinstance(value, str) else json.dumps(value)


def otel_span(trace: dict, obs: dict, by: timedelta) -> Span:
    md = obs.get("metadata") or {}
    attributes = dict(md.get("attributes") or {})
    if obs.get("input") is not None:
        attributes["langfuse.observation.input"] = as_attribute_text(obs["input"])
    if obs.get("output") is not None:
        attributes["langfuse.observation.output"] = as_attribute_text(obs["output"])
    if obs.get("level") not in (None, "DEFAULT"):
        attributes.setdefault("langfuse.observation.level", obs["level"])
    if obs.get("statusMessage"):
        attributes.setdefault("langfuse.observation.status_message", obs["statusMessage"])
    if not obs.get("parentObservationId") and trace.get("input") is not None:
        # The root span carried the trace's input (HolmesGPT's langfuse.trace.input).
        attributes["langfuse.trace.input"] = as_attribute_text(trace["input"])
    return Span(
        trace_id=bytes.fromhex(obs["traceId"]),
        span_id=bytes.fromhex(obs["id"]),
        parent_span_id=bytes.fromhex(obs["parentObservationId"]) if obs.get("parentObservationId") else b"",
        name=obs["name"] or "",
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=ns(shifted(obs["startTime"], by)),
        end_time_unix_nano=ns(shifted(obs.get("endTime") or obs["startTime"], by)),
        attributes=key_values(attributes),
    )


def is_agent_span(obs: dict) -> bool:
    """Spans HolmesGPT sent over OTLP (16-hex ids); the rest the driver added via the API."""
    return bool(re.fullmatch(r"[0-9a-f]{16}", obs["id"])) and "resourceAttributes" in (obs.get("metadata") or {})


def send_otlp(entry: dict, by: timedelta) -> int:
    """Re-send one investigation's agent spans to Langfuse's OTLP endpoint."""
    trace = entry["trace"]
    spans = [o for o in entry["observations"] if is_agent_span(o)]
    if not spans:
        return 0
    md = spans[0]["metadata"]
    resource = Resource(attributes=key_values(md.get("resourceAttributes") or {}))
    scope = InstrumentationScope(name=(md.get("scope") or {}).get("name") or "holmesgpt",
                                 version=(md.get("scope") or {}).get("version") or "")
    # Batches stay well under request-size limits; LLM inputs reach 100 KB each.
    batch, size = [], 0
    for obs in spans:
        span = otel_span(trace, obs, by)
        batch.append(span)
        size += span.ByteSize()
        if size > 3_000_000:
            _post_otlp(resource, scope, batch)
            batch, size = [], 0
    if batch:
        _post_otlp(resource, scope, batch)
    return len(spans)


def _post_otlp(resource: Resource, scope: InstrumentationScope, spans: list[Span]) -> None:
    request = ExportTraceServiceRequest(resource_spans=[
        ResourceSpans(resource=resource, scope_spans=[ScopeSpans(scope=scope, spans=spans)])
    ])
    token = base64.b64encode(":".join(AUTH).encode()).decode()
    response = httpx.post(f"{HOST}/api/public/otel/v1/traces", content=request.SerializeToString(),
                          headers={"Content-Type": "application/x-protobuf", "Authorization": f"Basic {token}"},
                          timeout=120)
    check(response, "OTLP export")


def driver_trace_event(entry: dict, by: timedelta) -> dict:
    """What the SREGym driver added to Holmes's trace: session, tags, metadata, answer."""
    trace = entry["trace"]
    body = {
        "id": trace["id"],
        "timestamp": shifted(trace["timestamp"], by),
        "environment": trace["environment"],
        "sessionId": trace.get("sessionId"),
        "tags": trace.get("tags") or [],
        "metadata": {k: v for k, v in (trace.get("metadata") or {}).items() if k not in OTEL_METADATA},
        "output": trace.get("output") or "",
    }
    return {"id": f"{trace['id']}-sregym-link", "type": "trace-create", "body": body}


def replay_investigations(entries: list[dict], by: timedelta) -> None:
    for entry in entries:
        trace = entry["trace"]
        spans = send_otlp(entry, by)
        ingest([driver_trace_event(entry, by)])
        md = trace.get("metadata") or {}
        say(f"  replayed {md.get('sregym_problem_id', trace['id']):47} {spans:4} spans  trace {trace['id']}")
    wait_for_traces(entries)


def wait_for_traces(entries: list[dict], timeout_s: int = 180) -> None:
    """Langfuse ingests asynchronously; wait until every replayed observation is queryable."""
    pending = {e["trace"]["id"]: {o["id"] for o in e["observations"] if is_agent_span(o)} for e in entries}
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        for trace_id, want in list(pending.items()):
            response = api.get(f"/traces/{trace_id}")
            if response.status_code == 200 and want <= {o["id"] for o in response.json().get("observations", [])}:
                del pending[trace_id]
        if pending:
            time.sleep(5)
    if pending:
        sys.exit(f"Langfuse has not finished ingesting {len(pending)} trace(s) after {timeout_s}s: {sorted(pending)}. "
                 "Check the langfuse-worker logs, then rerun this step (it is idempotent).")


# ---------------------------------------------------------------- scores and datasets

def import_scores(scores: list[dict]) -> int:
    """Re-create exported scores with their ids (re-running a step overwrites, never duplicates)."""
    events = []
    for s in scores:
        body = {
            "id": s["id"], "traceId": s["traceId"], "observationId": s.get("observationId"),
            "name": s["name"], "dataType": s["dataType"], "comment": s.get("comment"),
            "metadata": s.get("metadata") or None, "environment": s.get("environment"),
            "value": s["stringValue"] if s["dataType"] == "CATEGORICAL" else s["value"],
        }
        events.append({"id": f"score-{s['id']}", "type": "score-create", "body": {k: v for k, v in body.items() if v is not None}})
    ingest(events)
    return len(events)


def link_runs(run_names: list[str]) -> None:
    """Re-create the experiment runs: each run item links a trace to its dataset item."""
    for name in run_names:
        run = dataset_run(name)
        for item in run["items"]:
            check(api.post("/dataset-run-items", json={
                "runName": run["name"], "runDescription": run.get("description"), "metadata": run.get("metadata"),
                "datasetItemId": item["datasetItemId"], "traceId": item["traceId"], "observationId": item.get("observationId"),
            }), f"Link {item['datasetItemId']} in run {name}")
        say(f"  run {name:45} {len(run['items'])} item(s) on {run['datasetName']}")


def ensure_llm_connection() -> None:
    """The Langfuse LLM connection the managed evaluator uses (needs the key)."""
    check(api.put("/llm-connections", json={
        "provider": os.environ["LLM_CONNECTION_PROVIDER"], "adapter": "openai",
        "baseURL": os.environ["LLM_CONNECTION_BASE_URL"], "secretKey": LLM_KEY,
        "withDefaultModels": True,
    }), "LLM connection")
    say(f"  LLM connection '{os.environ['LLM_CONNECTION_PROVIDER']}' ({os.environ['LLM_CONNECTION_BASE_URL']})")


def script_env(**extra: str) -> dict:
    """Environment for the study's scripts, which read their settings from env vars."""
    return {**os.environ, "SDK_JUDGE_API_KEY": LLM_KEY, **extra}
