"""The evaluation: one function per step, each a `make` target of the same name.

Every step recreates what the study created at that point, in the order it was
created, then prints where to look in Langfuse and what it shows. Steps are
idempotent and refuse to run before the step they build on.
"""

import argparse
import os
import re
import subprocess
import sys
import time

from . import lf
from .lf import api, check, heading, say, ui

DEMO_DIR = __package__
# SREGym's own grades, written by publish_results.py from SREGym's results file.
SREGYM_GRADES = {"diagnosis-pass", "diagnosis-score", "diagnosis-localization", "diagnosis-characterization",
                 "diagnosis-scope", "run-complete", "time-to-diagnose-s",
                 "holmes-cost-usd", "holmes-input-tokens", "holmes-output-tokens", "holmes-llm-calls"}
SDK_JUDGE_SCORES = {"sdk-diagnosis-score", "sdk-diagnosis-pass", "sdk-diagnosis-localization",
                    "sdk-diagnosis-characterization", "sdk-diagnosis-scope"}
MANAGED_SCORE = "sregym"

# Where the study found something; the steps point at these.
PVC_RUN1_TRACE = "ab46e879c8bbf505a87c325a23542639"
PVC_RUN1_DISMISSAL_CALL = "facf3ee34ac5caa3"
NETPOL_RUN1_TRACE = "1cd7e4ad06fb8cedd19acc877f0490e3"
SMOKE_TRACE = "a2f7d8f60136802a19ad6544624643c0"


def run_script(script: str, *args: str, **env: str) -> None:
    command = [sys.executable, "-m", f"{DEMO_DIR}.{script}", *args]
    say(f"\n$ python {script}.py {' '.join(args)}")
    result = subprocess.run(command, env=lf.script_env(**env))
    if result.returncode != 0:
        sys.exit(f"{script}.py {' '.join(args)} failed (exit {result.returncode})")


def has_key(step: str) -> bool:
    if lf.LLM_KEY:
        say(f"OPENROUTER_API_KEY is set: {step} runs its LLM calls for real.")
        return True
    say(f"OPENROUTER_API_KEY is empty: {step} imports the study's saved LLM results instead.")
    return False


def dataset_id(name: str) -> str | None:
    response = api.get(f"/v2/datasets/{name}")
    return check(response, f"Dataset {name}")["id"] if response.status_code != 404 else None


def run_id(dataset: str, run: str) -> str | None:
    response = api.get(f"/datasets/{dataset}/runs/{run}")
    return check(response, f"Run {run}")["id"] if response.status_code != 404 else None


def compare_url(dataset: str, runs: list[str]) -> str:
    ids = [run_id(dataset, r) for r in runs]
    return ui(f"datasets/{dataset_id(dataset)}/compare?" + "&".join(f"runs={i}" for i in ids if i))


def table(rows: list[list], header: list[str]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for row in [header, *rows]:
        say("  " + "  ".join(str(x).ljust(w) for x, w in zip(row, widths)))


# ---------------------------------------------------------------- 1. load-traces

def load_traces() -> None:
    heading("What did the agent do? HolmesGPT's investigations, replayed through OTLP")
    model = lf.load("models.json")[0]
    if not any(m["modelName"] == model["modelName"] for m in lf.paged("/models")):
        check(api.post("/models", json={k: model[k] for k in ("modelName", "matchPattern", "unit", "inputPrice", "outputPrice")}),
              "Model price")
    say(f"Model price for {model['modelName']}: ${model['inputPrice'] * 1e6:.2f} / ${model['outputPrice'] * 1e6:.2f} per million tokens in / out")

    if lf.exists(f"/traces/{lf.anchor()['id']}"):
        by = lf.shift()
        say("Traces were loaded before; replaying again with the same time shift (same ids, nothing duplicates).")
    else:
        by = lf.new_shift()
    say(f"Time shift: {by} ({'newest investigation lands now' if by else 'original October 2026 timestamps'})")
    entries = lf.traces_of_runs(lf.RUNS_LOADED_BY["load-traces"])
    lf.replay_investigations(entries, by)

    rows = []
    real = {s["traceId"]: s["value"] for s in lf.scores_named({"holmes-cost-usd"}, {e["trace"]["id"] for e in entries})}
    for e in entries:
        loaded = check(api.get(f"/traces/{e['trace']['id']}"), "Read trace")
        md = e["trace"]["metadata"]
        langfuse_cost = loaded.get("totalCost") or 0
        rows.append([md["sregym_problem_id"], md["sregym_run_name"], len(loaded["observations"]),
                     f"${langfuse_cost:.2f}", f"${real[e['trace']['id']]:.2f}", f"{langfuse_cost / real[e['trace']['id']]:.0f}x"])
    say()
    table(rows, ["problem", "run", "observations", "Langfuse cost", "real cost", "over"])
    say(f"""
Open {ui('traces')} (environment: sregym)
  - Each trace is one investigation: every LLM call (gen_ai.chat) and every tool call, in order.
    Sessions group them by run; tags carry problem and model.
  - The failed PVC investigation, LLM call 2:
      {ui(f'traces/{PVC_RUN1_TRACE}?observation={PVC_RUN1_DISMISSAL_CALL}')}
    Its pod listing shows jaeger stuck Pending; its reasoning calls that "likely nothing to worry about".
  - network_policy_block: {ui(f'traces/{NETPOL_RUN1_TRACE}')}
    Holmes blamed a pod restart that was the benchmark's injected noise and never queried NetworkPolicies.
    Search its tool outputs for "[OOM]": HolmesGPT's tool memory limit killed ordinary kubectl calls.
  - Cost: Langfuse's total is far above the real cost because HolmesGPT's LLM spans report a
    running token total. The table's real cost is the corrected one from the study.
Next: make create-dataset""")


# ---------------------------------------------------------------- 2. create-dataset

def create_dataset() -> None:
    heading("What should it have found? sregym-lite, 17 SREGym problems with their root causes")
    data = lf.dataset(os.environ["LANGFUSE_DATASET"])
    if not dataset_id(data["name"]):
        check(api.post("/v2/datasets", json={"name": data["name"], "description": data.get("description"),
                                              "metadata": data.get("metadata")}), "Create dataset")
    for item in data["items"]:
        check(api.post("/dataset-items", json={
            "datasetName": data["name"], "id": item["id"], "input": item["input"],
            "expectedOutput": item["expectedOutput"], "metadata": item.get("metadata"),
        }), f"Item {item['id']}")
    example = next(i for i in data["items"] if i["id"].endswith(":network_policy_block"))
    say(f"""{len(data['items'])} items in {data['name']}.
  input           what the agent is told: app, namespaces, description ({', '.join(sorted(example['input']))})
  expected output the root cause SREGym injected, e.g. network_policy_block:
                  {str(example['expectedOutput'])[:160]}...
  metadata        the SREGym problem class, fault level and oracle settings

Open {ui(f'datasets/{dataset_id(data["name"])}')}
Next: make experiment-runs""")


# ---------------------------------------------------------------- 3. experiment-runs

def experiment_runs() -> None:
    heading("How often is it right, and at what cost? Experiment runs with SREGym's grades")
    lf.shift()  # fails with "run make load-traces first" if the traces are missing
    lf.require(bool(dataset_id(os.environ["LANGFUSE_DATASET"])), "create-dataset", "The sregym-lite dataset")
    runs = lf.RUNS_LOADED_BY["load-traces"]
    lf.link_runs(runs)
    trace_ids = {e["trace"]["id"] for e in lf.traces_of_runs(runs)}
    n = lf.import_scores(lf.scores_named(SREGYM_GRADES, trace_ids))
    say(f"  {n} SREGym grade scores imported (pass, score, localization, characterization, scope, time, corrected cost)")

    grades = {(s["traceId"], s["name"]): s["value"] for s in lf.scores_named(SREGYM_GRADES, trace_ids)}
    rows = []
    for e in lf.traces_of_runs(runs):
        t = e["trace"]["id"]
        md = e["trace"]["metadata"]
        rows.append([md["sregym_problem_id"], md["sregym_run_name"], md.get("holmes_max_steps"),
                     "pass" if grades.get((t, "diagnosis-pass")) else "fail", f"{grades.get((t, 'diagnosis-score'), 0):.2f}",
                     int(grades.get((t, "holmes-llm-calls"), 0)), f"${grades.get((t, 'holmes-cost-usd'), 0):.2f}"])
    say()
    table(rows, ["problem", "run", "step cap", "SREGym", "score", "LLM calls", "cost"])
    say(f"""
Open the runs side by side: {compare_url(os.environ['LANGFUSE_DATASET'], runs)}
  - duplicate_pvc_mounts_social_network failed at a 40-step cap and passed at 100: the step cap decided it.
  - Failures took more LLM calls and cost more: effort tracks failure.
Next: make offline-judges""")


# ---------------------------------------------------------------- 4. offline-judges

def create_sregym_evaluator() -> None:
    exported = next(e for e in lf.load("evaluators.json")["evaluators"] if e["name"] == MANAGED_SCORE)
    check(api.post("/unstable/evaluators", json={
        "type": "llm_as_judge", "name": exported["name"], "prompt": exported["prompt"],
        "outputDefinition": exported["outputDefinition"],
        "modelConfig": {"provider": os.environ["LLM_CONNECTION_PROVIDER"], "model": os.environ["MANAGED_JUDGE_MODEL"]},
    }), "Evaluator sregym")
    rules = lf.paged("/unstable/evaluation-rules")
    if any(r["name"] == MANAGED_SCORE for r in rules):
        say(f"  rule '{MANAGED_SCORE}' exists")
        return
    exported_rule = next(r for r in lf.load("evaluators.json")["rules"] if r["name"] == MANAGED_SCORE)
    check(api.post("/unstable/evaluation-rules", json={
        "name": MANAGED_SCORE, "evaluator": {"name": MANAGED_SCORE, "scope": "project", "type": "llm_as_judge"},
        "target": "experiment", "enabled": True, "sampling": 1,
        "filter": [{"type": "stringOptions", "column": "datasetId", "operator": "any of",
                    "value": [dataset_id(os.environ["LANGFUSE_DATASET"])]}],
        "mapping": exported_rule["mapping"],
    }), "Evaluation rule sregym")
    say(f"  evaluator and rule '{MANAGED_SCORE}': SREGym's 9-question checklist, on new experiment runs of sregym-lite")


def import_replay_runs() -> None:
    """The study's evaluator replay: Holmes's answers re-submitted as SDK experiment runs."""
    by = lf.shift()
    events = []
    for entry in lf.replay_traces():
        t = entry["trace"]
        events.append({"id": f"{t['id']}-trace", "type": "trace-create", "body": {
            "id": t["id"], "name": t["name"], "timestamp": lf.shifted(t["timestamp"], by), "environment": t["environment"],
            "input": t.get("input"), "output": t.get("output"), "metadata": t.get("metadata"),
        }})
        for o in entry["observations"]:
            events.append({"id": f"{o['id']}-span", "type": "span-create", "body": {
                "id": o["id"], "traceId": t["id"], "parentObservationId": o.get("parentObservationId"), "name": o["name"],
                "environment": t["environment"], "startTime": lf.shifted(o["startTime"], by),
                "endTime": lf.shifted(o.get("endTime") or o["startTime"], by), "input": o.get("input"), "output": o.get("output"),
                "metadata": {k: v for k, v in (o.get("metadata") or {}).items() if k not in lf.OTEL_METADATA},
            }})
    lf.ingest(events)
    replay_runs = [r["name"] for r in lf.load("dataset_runs.json") if r["name"].startswith("replay-")]
    time.sleep(10)
    lf.link_runs(replay_runs)
    n = lf.import_scores(lf.scores_named({MANAGED_SCORE}, {e["trace"]["id"] for e in lf.replay_traces()}))
    say(f"  {n} saved '{MANAGED_SCORE}' evaluator scores imported on the replay runs")


def offline_judges() -> None:
    heading("Can an LLM judge in Langfuse grade it? Three judges, one answer key")
    lf.require(bool(run_id(os.environ["LANGFUSE_DATASET"], "holmes-noise-run-1")), "experiment-runs", "The experiment runs")
    if has_key("offline-judges"):
        lf.ensure_llm_connection()
        create_sregym_evaluator()
        say("\nManaged evaluator: replay Holmes's 6 answers as new experiment runs; the 'sregym' rule scores them.")
        run_script("replay_judge_check", "holmes-noise-run-1", "holmes-noise-run-2-steps100",
                   DATASET_NAME=os.environ["LANGFUSE_DATASET"], EVALUATOR_SCORE=MANAGED_SCORE)
        say("\nSDK judge: run_batched_evaluation over the investigation traces, with a composite evaluator.")
        run_script("sdk_judge")
    else:
        import_replay_runs()
        n = lf.import_scores(lf.scores_named(SDK_JUDGE_SCORES, {e["trace"]["id"] for e in lf.traces_of_runs(lf.RUNS_LOADED_BY["load-traces"])}))
        say(f"  {n} saved SDK judge scores imported (D1-D3, weighted composite, pass)")
        saved = lf.load("scores.json.gz")
        sregym = {(s["traceId"], s["name"]): s["value"] for s in saved}
        replay_answer = {}  # (dataset item, source run) -> replay trace
        for run in (r for r in lf.load("dataset_runs.json") if r["name"].startswith("replay-")):
            source_run = re.search(r"from '([^']+)'", run["description"]).group(1)
            for item in run["items"]:
                replay_answer[item["datasetItemId"], source_run] = item["traceId"]
        rows = []
        for e in lf.traces_of_runs(lf.RUNS_LOADED_BY["load-traces"]):
            t, md = e["trace"]["id"], e["trace"]["metadata"]
            managed = sregym.get((replay_answer.get((f"sregym-lite:{md['sregym_problem_id']}", md["sregym_run_name"])), MANAGED_SCORE))
            rows.append([md["sregym_problem_id"], md["sregym_run_name"], f"{sregym.get((t, 'diagnosis-score'), 0):.2f}",
                         f"{managed:.2f}" if managed is not None else "-", f"{sregym.get((t, 'sdk-diagnosis-score'), 0):.2f}"])
        say()
        table(rows, ["problem", "run", "SREGym", "managed 'sregym'", "SDK judge"])
    say(f"""
Open {ui('evals')} for the evaluator and its rule (with a key), and the scores on each trace.
  - Three judges, one checklist: SREGym's own judge, a Langfuse-managed evaluator, an SDK
    run_batched_evaluation judge. They agree on pass/fail; per-dimension scores differ.
  - The managed evaluator only scores runs created after its rule exists, so the study replayed
    Holmes's answers as new runs ({ui(f"datasets/{dataset_id(os.environ['LANGFUSE_DATASET'])}")} -> Runs, "replay-...").
That is the whole evaluation. docs/evaluation.md lists why the agent failed.""")


# ---------------------------------------------------------------- helpers

STEPS = {
    "load-traces": load_traces,
    "create-dataset": create_dataset,
    "experiment-runs": experiment_runs,
    "offline-judges": offline_judges,
}


def status() -> None:
    heading("What is loaded")
    dataset = os.environ["LANGFUSE_DATASET"]
    done = {
        "load-traces": lf.exists(f"/traces/{lf.anchor()['id']}"),
        "create-dataset": bool(dataset_id(dataset)),
        "experiment-runs": bool(dataset_id(dataset) and run_id(dataset, "holmes-noise-run-1")),
        "offline-judges": bool(lf.paged("/v2/scores", {"name": "sdk-diagnosis-score"})),
    }
    for step, ok in done.items():
        say(f"  [{'x' if ok else ' '}] make {step}")
    pending = [s for s, ok in done.items() if not ok]
    say(f"\nNext: make {pending[0]}" if pending else "\nEvery step is loaded.")
    say(f"Langfuse: {lf.UI_URL}")


def ask() -> None:
    heading("Smoke test: replay one investigation through OTLP")
    by = lf.shift() if lf.exists(f"/traces/{lf.anchor()['id']}") else lf.new_shift()
    entry = next(t for t in lf.load("traces.json.gz") if t["trace"]["id"] == SMOKE_TRACE)
    lf.replay_investigations([entry], by)
    say(f"Open {ui(f'traces/{SMOKE_TRACE}')}")


def run_all() -> None:
    for step in STEPS.values():
        step()


def main() -> None:
    parser = argparse.ArgumentParser(prog="demo", description=__doc__.split("\n")[0])
    parser.add_argument("step", choices=[*STEPS, "all", "status", "ask"])
    args = parser.parse_args()
    {**STEPS, "all": run_all, "status": status, "ask": ask}[args.step]()
