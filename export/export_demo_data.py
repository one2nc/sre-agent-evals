"""Export the SREGym + HolmesGPT work from a Langfuse project into data/.

Maintainer-only: run once against the Langfuse that holds the original runs
(`make export`). Everything goes through Langfuse's public API, so the source can
be any Langfuse v3 instance.

The source project also holds the study's online work (live checks, review
queue, post-incident grading, prompt loop, online judge). The export keeps only
the offline evaluation: the runs in EXPORT_RUNS, the traces they link, and the
scores and evaluators named in EXPORT_SCORES. The Makefile sets both lists.

Writes:
    traces.json.gz        the traces the exported runs link (investigations in
                          environment sregym, evaluator replays in sdk-experiment),
                          each with its full observations
    scores.json.gz        the EXPORT_SCORES scores on those traces
    datasets.json         the EXPORT_DATASET dataset with its items
    dataset_runs.json     the EXPORT_RUNS runs with their run items
    evaluators.json       the EXPORT_SCORES LLM-as-a-judge evaluators and their rules
    models.json           custom model price definitions
    manifest.json         counts per file, source versions, export time

Fails before writing anything if an API key, a private-network IP or a home
directory path appears in the exported data.
"""

import gzip
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

REQUIRED_ENV = [
    "EXPORT_LANGFUSE_HOST",
    "EXPORT_LANGFUSE_PUBLIC_KEY",
    "EXPORT_LANGFUSE_SECRET_KEY",
    "EXPORT_DATASET",
    "EXPORT_RUNS",
    "EXPORT_SCORES",
    "EXPORT_DIR",
]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    sys.exit(f"Missing required env vars: {', '.join(missing)}")

OUT = Path(os.environ["EXPORT_DIR"])
DATASET = os.environ["EXPORT_DATASET"]
# The Makefile wraps these long lists over several lines, which leaves spaces.
RUNS = [name.strip() for name in os.environ["EXPORT_RUNS"].split(",")]
SCORES = {name.strip() for name in os.environ["EXPORT_SCORES"].split(",")}

api = httpx.Client(
    base_url=f"{os.environ['EXPORT_LANGFUSE_HOST'].rstrip('/')}/api/public",
    auth=(os.environ["EXPORT_LANGFUSE_PUBLIC_KEY"], os.environ["EXPORT_LANGFUSE_SECRET_KEY"]),
    timeout=120,
)

# Things that must never leave this machine in a shared demo.
SECRET_PATTERNS = {
    "OpenRouter key": re.compile(r"sk-or-[A-Za-z0-9-]{10,}"),
    "Langfuse secret key": re.compile(r"sk-lf-[0-9a-f-]{20,}"),
    "Langfuse public key": re.compile(r"pk-lf-[0-9a-f-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "private key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "LAN IP": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "home directory": re.compile(r"/home/[a-z][a-z0-9_-]*/"),
}


def get(path: str, params: dict | None = None) -> dict:
    response = api.get(path, params=params)
    if response.is_error:
        sys.exit(f"GET {path} failed: HTTP {response.status_code}\n{response.text[:500]}")
    return response.json()


def paged(path: str, params: dict | None = None, limit: int = 100) -> list[dict]:
    rows, page = [], 1
    while True:
        body = get(path, {**(params or {}), "page": page, "limit": limit})
        rows += body["data"]
        if page >= body["meta"].get("totalPages", 1):
            return rows
        page += 1


# Attributes that identify the source project rather than describe the agent:
# the Langfuse SDK puts the project's public key on every span's resource.
DROPPED_KEYS = {"public_key"}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in DROPPED_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


# What the post-incident grading wrote onto the investigation traces in the source
# project. It is online work, so the export leaves it out.
POSTMORTEM_TAG = "postmortem"
POSTMORTEM_METADATA_PREFIX = "postmortem_"


def without_postmortem(trace: dict) -> dict:
    trace["tags"] = [t for t in trace.get("tags") or [] if t != POSTMORTEM_TAG]
    trace["metadata"] = {k: v for k, v in (trace.get("metadata") or {}).items()
                         if not k.startswith(POSTMORTEM_METADATA_PREFIX)}
    return trace


def export_traces(trace_ids: set[str]) -> list[dict]:
    traces = []
    for trace_id in sorted(trace_ids):
        # The single-trace endpoint returns every observation in full.
        trace = get(f"/traces/{trace_id}")
        observations = trace.pop("observations")
        trace.pop("scores", None)
        traces.append(scrub({"trace": without_postmortem(trace),
                             "observations": sorted(observations, key=lambda o: o["startTime"])}))
    return sorted(traces, key=lambda t: t["trace"]["timestamp"])


def export_scores(trace_ids: set[str]) -> list[dict]:
    scores = []
    for trace_id in sorted(trace_ids):
        scores += [s for s in paged("/v2/scores", {"traceId": trace_id}) if s["name"] in SCORES]
    return sorted(scores, key=lambda s: (s["traceId"], s["name"], s["timestamp"]))


def export_dataset() -> tuple[dict, list[dict]]:
    dataset = get(f"/v2/datasets/{DATASET}")
    dataset["items"] = sorted(paged("/dataset-items", {"datasetName": DATASET}), key=lambda i: i["id"])
    runs = []
    for name in RUNS:
        run = get(f"/datasets/{DATASET}/runs/{name}")
        run.pop("datasetRunItems", None)
        run["datasetName"] = DATASET
        run["items"] = paged("/dataset-run-items", {"datasetId": dataset["id"], "runName": name})
        runs.append(run)
    return dataset, sorted(runs, key=lambda r: r["createdAt"])


def export_evaluators() -> dict:
    evaluators = [e for e in paged("/unstable/evaluators") if e.get("scope") == "project" and e["name"] in SCORES]
    rules = [r for r in paged("/unstable/evaluation-rules") if r["name"] in SCORES]
    return {"evaluators": evaluators, "rules": rules}


def export_models() -> list[dict]:
    return [m for m in paged("/models") if not m.get("isLangfuseManaged")]


def secret_scan(files: dict[str, object]) -> None:
    hits = []
    for name, payload in files.items():
        text = json.dumps(payload)
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                hits.append(f"{name}: {label}: …{text[max(0, match.start() - 40):match.end() + 10]}…")
    if hits:
        sys.exit("Refusing to export; sensitive strings found:\n" + "\n".join(hits[:30]))


def main() -> None:
    dataset, runs = export_dataset()
    trace_ids = {item["traceId"] for run in runs for item in run["items"]}
    traces = export_traces(trace_ids)
    files = {
        "traces.json.gz": traces,
        "scores.json.gz": export_scores(trace_ids),
        "datasets.json": [dataset],
        "dataset_runs.json": runs,
        "evaluators.json": export_evaluators(),
        "models.json": export_models(),
    }
    secret_scan(files)

    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        text = json.dumps(payload, indent=1, sort_keys=True)
        if name.endswith(".gz"):
            # mtime=0 keeps the file byte-identical when the data has not changed.
            with gzip.GzipFile(OUT / name, "wb", mtime=0) as f:
                f.write(text.encode("utf-8"))
        else:
            (OUT / name).write_text(text + "\n")

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_langfuse_version": os.environ.get("EXPORT_SOURCE_LANGFUSE_VERSION", "unknown"),
        "holmesgpt_commit": os.environ.get("EXPORT_HOLMESGPT_COMMIT", "unknown"),
        "sregym_commit": os.environ.get("EXPORT_SREGYM_COMMIT", "unknown"),
        "counts": {
            "traces": len(traces),
            "traces_by_environment": dict(Counter(t["trace"]["environment"] for t in traces)),
            "observations": sum(len(t["observations"]) for t in traces),
            "scores": len(files["scores.json.gz"]),
            "scores_by_name": {},
            "dataset_items": {dataset["name"]: len(dataset["items"])},
            "dataset_runs": {r["name"]: len(r["items"]) for r in runs},
            "evaluators": len(files["evaluators.json"]["evaluators"]),
            "models": len(files["models.json"]),
        },
    }
    for score in files["scores.json.gz"]:
        by_name = manifest["counts"]["scores_by_name"]
        by_name[score["name"]] = by_name.get(score["name"], 0) + 1
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(json.dumps(manifest["counts"], indent=1))


if __name__ == "__main__":
    main()
