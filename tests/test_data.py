"""data/ holds everything the evaluation steps look up, and nothing from the online work.

Runs without Langfuse or Docker: `make test`. Run it after every `make export`,
because a wrong EXPORT_RUNS or EXPORT_SCORES list otherwise shows up only as a
crash or an empty table in the middle of `make all`.
"""

import gzip
import json
import re
import unittest
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# What demo/steps.py and demo/lf.py look up.
DATASET = "sregym-lite"
AGENT_RUNS = ["holmes-noise-run-1", "holmes-noise-run-2-steps100"]
SREGYM_GRADES = {"diagnosis-pass", "diagnosis-score", "diagnosis-localization", "diagnosis-characterization",
                 "diagnosis-scope", "run-complete", "time-to-diagnose-s",
                 "holmes-cost-usd", "holmes-input-tokens", "holmes-output-tokens", "holmes-llm-calls"}
SDK_JUDGE_SCORES = {"sdk-diagnosis-score", "sdk-diagnosis-pass", "sdk-diagnosis-localization",
                    "sdk-diagnosis-characterization", "sdk-diagnosis-scope"}
MANAGED_SCORE = "sregym"


def load(name: str):
    if name.endswith(".gz"):
        with gzip.open(DATA / name, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads((DATA / name).read_text())


TRACES = {t["trace"]["id"]: t["trace"] for t in load("traces.json.gz")}
RUNS = {r["name"]: r for r in load("dataset_runs.json")}
SCORES = load("scores.json.gz")


def trace_ids_of(run_names: list[str]) -> set[str]:
    return {item["traceId"] for name in run_names for item in RUNS[name]["items"]}


def replay_runs() -> dict[str, str]:
    """Agent run -> the run that replays its answers for the managed evaluator."""
    return {re.search(r"from '([^']+)'", r["description"]).group(1): name
            for name, r in RUNS.items() if name.startswith("replay-")}


class DataTest(unittest.TestCase):
    def test_dataset_items_have_root_causes(self):
        dataset = next(d for d in load("datasets.json") if d["name"] == DATASET)
        self.assertEqual(len(dataset["items"]), 17)
        self.assertTrue(all(item["expectedOutput"] for item in dataset["items"]))

    def test_every_run_item_links_an_exported_trace_and_dataset_item(self):
        item_ids = {i["id"] for d in load("datasets.json") for i in d["items"]}
        for run in RUNS.values():
            for item in run["items"]:
                self.assertIn(item["traceId"], TRACES, f"run {run['name']}")
                self.assertIn(item["datasetItemId"], item_ids, f"run {run['name']}")

    def test_agent_runs_are_sessions_of_their_traces(self):
        for name in AGENT_RUNS:
            self.assertIn(name, RUNS)
            self.assertTrue(all(TRACES[t]["sessionId"] == name for t in trace_ids_of([name])), name)

    def test_each_agent_run_has_one_replay_with_the_same_items(self):
        replays = replay_runs()
        self.assertEqual(sorted(replays), sorted(AGENT_RUNS))
        for agent_run, replay in replays.items():
            self.assertEqual({i["datasetItemId"] for i in RUNS[replay]["items"]},
                             {i["datasetItemId"] for i in RUNS[agent_run]["items"]})

    def test_each_investigation_has_grades_and_sdk_judge_scores(self):
        names_by_trace = {}
        for s in SCORES:
            names_by_trace.setdefault(s["traceId"], set()).add(s["name"])
        for trace_id in trace_ids_of(AGENT_RUNS):
            missing = (SREGYM_GRADES | SDK_JUDGE_SCORES) - names_by_trace.get(trace_id, set())
            self.assertFalse(missing, f"trace {trace_id}")
        for trace_id in trace_ids_of(list(replay_runs().values())):
            self.assertIn(MANAGED_SCORE, names_by_trace.get(trace_id, set()), f"replay trace {trace_id}")

    def test_managed_evaluator_and_rule_are_exported(self):
        evaluators = load("evaluators.json")
        self.assertIn(MANAGED_SCORE, [e["name"] for e in evaluators["evaluators"]])
        self.assertIn(MANAGED_SCORE, [r["name"] for r in evaluators["rules"]])

    def test_no_online_data(self):
        self.assertEqual(set(TRACES), trace_ids_of(list(RUNS)))
        self.assertEqual({s["name"] for s in SCORES} - SREGYM_GRADES - SDK_JUDGE_SCORES - {MANAGED_SCORE}, set())
        self.assertFalse([t for t in TRACES.values() if "postmortem" in (t.get("tags") or [])])


if __name__ == "__main__":
    unittest.main()
