"""SREGym's diagnosis judge as Langfuse SDK evaluators, run on existing traces.

Uses `langfuse.run_batched_evaluation` to score HolmesGPT traces already in
Langfuse (tag and environment `sregym`), without re-running anything:

    mapper              trace -> output (the diagnosis) + expected output (the root
                        cause, read from the trace's `sregym-lite` dataset item)
    checklist_judge     one LLM call answers SREGym's 9 checklist questions and
                        returns three scores, one per dimension: share of Yes answers
    sregym_composite    composite_evaluator: SREGym's weighted composite and pass,
                        computed in code from the three dimension scores

Questions, hints, dimension weights and the pass threshold are read from
SREGym's own rca_checklists.yaml, so they match SREGym by construction. The
prompt wording is this file's: SREGym's in-context examples are not copied.

Then prints, per trace, SREGym's own grade, these SDK scores and the managed
`sregym` evaluator's score on the replayed answer (from replay_judge_check.py).

Re-running adds a new set of `sdk-*` scores to each trace; the comparison reads
the newest.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml
from langfuse import get_client
from langfuse.batch_evaluation import EvaluatorInputs
from langfuse.experiment import Evaluation
from openai import OpenAI

REQUIRED_ENV = [
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_DATASET",
    "SDK_JUDGE_CHECKLIST_FILE",
    "SDK_JUDGE_BASE_URL",
    "SDK_JUDGE_API_KEY",
    "SDK_JUDGE_MODEL",
]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    sys.exit(f"Missing required env vars: {', '.join(missing)}")

DATASET = os.environ["LANGFUSE_DATASET"]
# SREGym's sregym/conductor/oracles/llm_as_a_judge/rca_checklists.yaml (MIT), shipped in data/.
CHECKLIST_PATH = Path(os.environ["SDK_JUDGE_CHECKLIST_FILE"])
CHECKLIST = yaml.safe_load(CHECKLIST_PATH.read_text())
WEIGHTS = CHECKLIST["scoring"]["weights"]
THRESHOLD = CHECKLIST["scoring"]["threshold"]
QUESTION_IDS = [q["id"] for d in CHECKLIST["dimensions"] for q in d["questions"]]
DIMENSION_SCORES = {"D1": "sdk-diagnosis-localization", "D2": "sdk-diagnosis-characterization", "D3": "sdk-diagnosis-scope"}
MANAGED_SCORE = "sregym"
# Holmes's trace has no output when the CLI crashed; SREGym then graded the
# driver's placeholder, so the judge sees that same text.
FAILED_PLACEHOLDER = "HolmesGPT failed to produce an answer (exit 1)."
HOLMES_TRACES = json.dumps([
    {"type": "arrayOptions", "column": "tags", "operator": "any of", "value": ["sregym"]},
    {"type": "stringOptions", "column": "environment", "operator": "any of", "value": ["sregym"]},
])

langfuse = get_client()
api = httpx.Client(
    base_url=f"{os.environ['LANGFUSE_HOST'].rstrip('/')}/api/public",
    auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
    timeout=60,
)
judge = OpenAI(base_url=os.environ["SDK_JUDGE_BASE_URL"], api_key=os.environ["SDK_JUDGE_API_KEY"])

SYSTEM_PROMPT = f"""\
You are an expert SRE evaluator assessing an AI agent's root cause analysis.

You are given the ground-truth root cause, the agent's diagnosis, and a checklist
of {len(QUESTION_IDS)} Yes/No questions grouped into {len(CHECKLIST["dimensions"])} dimensions.

For each dimension: re-read its definition and hints, gather the diagnosis's
claims relevant to it, compare them with the ground truth (the authoritative
reference), then answer each question.

Rules:
- Answer based ONLY on what is explicitly stated in the diagnosis.
- Do not infer information not present in the provided texts.
- "Yes" always means the positive or correct outcome is present.
- Treat each question independently.
- evidence: a quote or paraphrase of at most 30 words supporting the answer.
"""

ANSWER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "checklist",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["answers"],
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "answer", "evidence", "confidence"],
                        "properties": {
                            "id": {"type": "string", "enum": QUESTION_IDS},
                            "answer": {"type": "string", "enum": ["Yes", "No"]},
                            "evidence": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        },
                    },
                }
            },
        },
    },
}


def user_message(diagnosis: str, root_cause: str) -> str:
    """Same layout as SREGym's DiagnosisJudge._build_user_message."""
    lines = ["## Ground-Truth Root Cause", root_cause, "", "## Agent Diagnosis", diagnosis, "", "## Evaluation Checklist"]
    for dim in CHECKLIST["dimensions"]:
        lines += [f"\n### {dim['id']} — {dim['name']}", dim["definition"].strip()]
        for q in dim["questions"]:
            lines.append(f"{q['id']}: {q['text']}")
            if q.get("evaluator_hint"):
                lines.append(f"  Hint: {q['evaluator_hint']}")
    lines += ["", f"Answer all {len(QUESTION_IDS)} questions."]
    return "\n".join(lines)


def ask_judge(diagnosis: str, root_cause: str) -> dict[str, dict]:
    last_error = None
    for _ in range(2):
        response = judge.chat.completions.create(
            model=os.environ["SDK_JUDGE_MODEL"],
            temperature=0,
            response_format=ANSWER_SCHEMA,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_message(diagnosis, root_cause)}],
        )
        try:
            answers = {a["id"]: a for a in json.loads(response.choices[0].message.content)["answers"]}
            if set(answers) == set(QUESTION_IDS):
                return answers
            last_error = f"answered {sorted(answers)}"
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            last_error = repr(exc)
    raise RuntimeError(f"judge did not answer all {len(QUESTION_IDS)} questions: {last_error}")


_expected_cache: dict[str, str] = {}


def expected_root_cause(problem_id: str) -> str:
    if problem_id not in _expected_cache:
        item = api.get(f"/dataset-items/{DATASET}:{problem_id}").raise_for_status().json()
        causes = item["expectedOutput"]
        _expected_cache[problem_id] = "\n\n".join(causes) if isinstance(causes, list) else str(causes)
    return _expected_cache[problem_id]


def mapper(*, item, **kwargs) -> EvaluatorInputs:
    metadata = item.metadata or {}
    problem_id = metadata["sregym_problem_id"]
    output = item.output if isinstance(item.output, str) and item.output.strip() else FAILED_PLACEHOLDER
    return EvaluatorInputs(
        input=item.input,
        output=output,
        expected_output=expected_root_cause(problem_id),
        metadata={"problem_id": problem_id, "run_name": metadata.get("sregym_run_name")},
    )


def checklist_judge(*, input, output, expected_output, metadata, **kwargs):
    if not (output or "").strip():  # SREGym scores an empty diagnosis 0 without asking
        return [Evaluation(name=n, value=0.0, data_type="NUMERIC", comment="empty diagnosis") for n in DIMENSION_SCORES.values()]
    answers = ask_judge(output, expected_output)
    evaluations = []
    for dim in CHECKLIST["dimensions"]:
        dim_answers = [answers[q["id"]] for q in dim["questions"]]
        yes = sum(a["answer"] == "Yes" for a in dim_answers)
        evaluations.append(Evaluation(
            name=DIMENSION_SCORES[dim["id"]],
            value=round(yes / len(dim_answers), 2),  # SREGym rounds dimension scores to 2 places
            data_type="NUMERIC",
            comment="; ".join(f"{a['id']} {a['answer']}: {a['evidence']}" for a in dim_answers),
            metadata={"answers": dim_answers, "judge_model": os.environ["SDK_JUDGE_MODEL"]},
        ))
    return evaluations


def sregym_composite(*, evaluations, **kwargs):
    scores = {e.name: e.value for e in evaluations}
    if not all(name in scores for name in DIMENSION_SCORES.values()):
        return []
    composite = round(sum(WEIGHTS[dim] * scores[name] for dim, name in DIMENSION_SCORES.items()), 2)
    passed = composite >= THRESHOLD
    parts = ", ".join(f"{WEIGHTS[d]}×{scores[n]}" for d, n in DIMENSION_SCORES.items())
    return [
        Evaluation(name="sdk-diagnosis-score", value=composite, data_type="NUMERIC", comment=f"{parts} = {composite}"),
        Evaluation(name="sdk-diagnosis-pass", value=1 if passed else 0, data_type="BOOLEAN",
                   comment=f"composite {composite} {'>=' if passed else '<'} threshold {THRESHOLD}"),
    ]


def latest_scores(trace_id: str) -> dict[str, float]:
    data = api.get("/v2/scores", params={"traceId": trace_id, "limit": 100}).raise_for_status().json()["data"]
    newest: dict[str, dict] = {}
    for s in data:
        if s["name"] not in newest or s["timestamp"] > newest[s["name"]]["timestamp"]:
            newest[s["name"]] = s
    return {name: s["value"] for name, s in newest.items()}


def managed_scores() -> dict[tuple[str, str], float]:
    """(source run, problem) -> the managed evaluator's score on the newest replay of it."""
    runs = api.get(f"/datasets/{DATASET}/runs", params={"limit": 100}).raise_for_status().json()["data"]
    newest_replay: dict[str, dict] = {}
    for run in runs:
        if run["name"].startswith("replay-"):
            source = run["name"][len("replay-"):].rsplit("-", 1)[0]
            if source not in newest_replay or run["createdAt"] > newest_replay[source]["createdAt"]:
                newest_replay[source] = run
    result = {}
    for source, run in newest_replay.items():
        detail = api.get(f"/datasets/{DATASET}/runs/{run['name']}").raise_for_status().json()
        for ri in detail["datasetRunItems"]:
            value = latest_scores(ri["traceId"]).get(MANAGED_SCORE)
            if value is not None:
                result[(source, ri["datasetItemId"].split(":", 1)[1])] = value
    return result


def compare() -> None:
    traces = api.get("/traces", params={"filter": HOLMES_TRACES, "limit": 100}).raise_for_status().json()["data"]
    managed = managed_scores()
    verdict = lambda v: "pass" if v is not None and v >= THRESHOLD else "fail"
    print(f"\n{'problem':47} {'run':28} | {'SREGym':>6} {'SDK':>5} {'managed':>7} | SDK D1/D2/D3 | verdicts SREGym/SDK/managed")
    agree_sdk = agree_managed = n = 0
    for t in sorted(traces, key=lambda t: (t["metadata"].get("sregym_run_name", ""), t["metadata"].get("sregym_problem_id", ""))):
        problem, run = t["metadata"]["sregym_problem_id"], t["metadata"]["sregym_run_name"]
        s = latest_scores(t["id"])
        sregym, sdk, man = s.get("diagnosis-score"), s.get("sdk-diagnosis-score"), managed.get((run, problem))
        dims = "/".join(f"{s.get(n, float('nan')):.2f}" for n in DIMENSION_SCORES.values())
        fmt = lambda v, w: f"{v:>{w}.2f}" if v is not None else f"{'-':>{w}}"
        print(f"{problem:47} {run:28} | {fmt(sregym, 6)} {fmt(sdk, 5)} {fmt(man, 7)} | {dims:14} | "
              f"{verdict(sregym)}/{verdict(sdk)}/{verdict(man) if man is not None else '-'}")
        n += 1
        agree_sdk += verdict(sregym) == verdict(sdk)
        agree_managed += man is not None and verdict(sregym) == verdict(man)
    print(f"\npass/fail agreement with SREGym: SDK {agree_sdk}/{n}, managed {agree_managed}/{sum(1 for k in managed)}")


def main() -> None:
    if not langfuse.auth_check():
        sys.exit("Langfuse auth failed. Check LANGFUSE_* settings.")
    print(f"Checklist {CHECKLIST.get('version')} from {CHECKLIST_PATH}: weights {WEIGHTS}, threshold {THRESHOLD}")
    result = langfuse.run_batched_evaluation(
        scope="traces",
        filter=HOLMES_TRACES,
        fetch_trace_fields="core,io",
        mapper=mapper,
        evaluators=[checklist_judge],
        composite_evaluator=sregym_composite,
        max_concurrency=3,
        metadata={"evaluator": "sdk_judge.py", "judge_model": os.environ["SDK_JUDGE_MODEL"]},
    )
    langfuse.flush()
    print(f"fetched {result.total_items_fetched}, processed {result.total_items_processed}, "
          f"failed {result.total_items_failed}, scores {result.total_scores_created} "
          f"+ composite {result.total_composite_scores_created}, evaluator failures {result.total_evaluations_failed}")
    time.sleep(10)  # scores are ingested asynchronously
    compare()


if __name__ == "__main__":
    main()
