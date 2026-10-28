"""Check a Langfuse-managed evaluator against SREGym's own judge.

Replays answers HolmesGPT already gave in earlier SREGym runs as new experiment
runs on the dataset, so any evaluation rule on that dataset scores them, then
puts the evaluator's score next to SREGym's `diagnosis-score` on the original
trace. Nothing is re-run: no cluster, no HolmesGPT, only the evaluator's calls.

Usage: replay_judge_check.py <source run> [<source run> ...]
Env:   LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, DATASET_NAME,
       EVALUATOR_SCORE (the evaluator's score name, e.g. sregym)
"""

import os
import sys
import time

import httpx
from langfuse import get_client

REQUIRED_ENV = ["LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "DATASET_NAME", "EVALUATOR_SCORE"]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    sys.exit(f"Missing required env vars: {', '.join(missing)}")

DATASET = os.environ["DATASET_NAME"]
EVALUATOR_SCORE = os.environ["EVALUATOR_SCORE"]
PASS_AT = 0.70  # SREGym's pass threshold
SCORE_WAIT_S = 600

langfuse = get_client()
api = httpx.Client(
    base_url=f"{os.environ['LANGFUSE_HOST'].rstrip('/')}/api/public",
    auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
    timeout=60,
)


def scores_on(trace_id: str) -> dict:
    response = api.get("/v2/scores", params={"traceId": trace_id, "limit": 100})
    response.raise_for_status()
    return {s["name"]: s for s in response.json()["data"]}


def source_answers(run_name: str) -> dict[str, dict]:
    """item id -> the answer SREGym graded, and SREGym's score for it."""
    response = api.get(f"/datasets/{DATASET}/runs/{run_name}")
    response.raise_for_status()
    answers = {}
    for ri in response.json()["datasetRunItems"]:
        trace = api.get(f"/traces/{ri['traceId']}").json()
        output = trace.get("output")
        sregym = scores_on(ri["traceId"])
        answers[ri["datasetItemId"]] = {
            # Holmes's trace has no output when it crashed; SREGym then graded the
            # driver's placeholder, so replay that same text.
            "answer": output if isinstance(output, str) and output.strip()
            else "HolmesGPT failed to produce an answer (exit 1).",
            "sregym_score": (sregym.get("diagnosis-score") or {}).get("value"),
            "sregym_pass": (sregym.get("diagnosis-pass") or {}).get("value"),
        }
    return answers


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    items_by_id = {i.id: i for i in langfuse.get_dataset(DATASET).items}
    stamp = int(time.time())
    pending = []  # (source run, item id, source, replay trace id)

    for source_run in sys.argv[1:]:
        answers = source_answers(source_run)
        result = langfuse.run_experiment(
            name="evaluator-replay",
            run_name=f"replay-{source_run}-{stamp}",
            description=f"Replays HolmesGPT's answers from '{source_run}' to check the '{EVALUATOR_SCORE}' evaluator. Not a new Holmes run.",
            data=[items_by_id[item_id] for item_id in answers],
            task=lambda *, item, **kwargs: answers[item.id]["answer"],
        )
        langfuse.flush()
        for item_result in result.item_results:
            item_id = item_result.item.id
            pending.append((source_run, item_id, answers[item_id], item_result.trace_id))
        print(f"replayed {len(answers)} answers from '{source_run}' as run '{result.run_name}'")

    print(f"waiting up to {SCORE_WAIT_S}s for '{EVALUATOR_SCORE}' scores...")
    deadline = time.monotonic() + SCORE_WAIT_S
    got: dict[str, dict] = {}
    while time.monotonic() < deadline and len(got) < len(pending):
        for _, _, _, trace_id in pending:
            if trace_id not in got and (s := scores_on(trace_id).get(EVALUATOR_SCORE)):
                got[trace_id] = s
        if len(got) < len(pending):
            time.sleep(15)

    print(f"\n{'problem':47} {'source run':28} {'SREGym':>6} {'eval':>6} {'diff':>6}  verdicts")
    agree = 0
    for source_run, item_id, src, trace_id in pending:
        problem = item_id.split(":", 1)[1]
        s = got.get(trace_id)
        if not s:
            print(f"{problem:47} {source_run:28} {src['sregym_score']:>6.2f} {'none':>6}")
            continue
        value = s["value"]
        same = (value >= PASS_AT) == (src["sregym_score"] >= PASS_AT)
        agree += same
        print(f"{problem:47} {source_run:28} {src['sregym_score']:>6.2f} {value:>6.2f} {value - src['sregym_score']:>+6.2f}  "
              f"{'agree' if same else 'DISAGREE'}  trace {trace_id}")
    print(f"\nscored {len(got)}/{len(pending)}; pass/fail agreement {agree}/{len(got)}")


if __name__ == "__main__":
    main()
