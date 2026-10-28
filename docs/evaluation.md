# How we evaluated the agent

We asked four questions about HolmesGPT, an AI SRE agent, and answered each one
with Langfuse. Each section below covers one question:
- what we did in the study,
- what the `make` step loads into your Langfuse,
- what to look at, and which Langfuse features answer the question.

Run the steps in this order, because a step refuses to run before the step it
builds on. The last section lists why the agent failed, and where each failure
shows in Langfuse.

## Contents

- [What did the agent do in each investigation?](#what-did-the-agent-do-in-each-investigation)
- [What should the agent have found?](#what-should-the-agent-have-found)
- [How often is the agent right, and at what cost?](#how-often-is-the-agent-right-and-at-what-cost)
- [Can an LLM judge in Langfuse grade the agent?](#can-an-llm-judge-in-langfuse-grade-the-agent)
- [Why the agent failed](#why-the-agent-failed)

## What did the agent do in each investigation?

Command: `make load-traces`

You can't judge an answer without the steps that led to it. In the study,
HolmesGPT sent OpenTelemetry traces straight to Langfuse. Our SREGym driver tagged
each trace with the problem, the model and the run, and it grouped each run as a
Langfuse session.

The step adds the model price, and then it replays the 6 investigations of the
runs `holmes-noise-run-1` and `holmes-noise-run-2-steps100`. The agent's spans go
to Langfuse's OpenTelemetry endpoint with their original IDs, timestamps and
attributes, so Langfuse ingests what it got from the agent. The driver's tags,
session and final answer go through Langfuse's ingestion API, as the driver sent
them.

What to look at:
- Tracing, then Traces, with environment `sregym`. Each trace is one
  investigation, with every LLM call and tool call in order.
- The failed PVC investigation, LLM call 2. Its pod listing shows the `jaeger`
  pod stuck in Pending, and the agent's reasoning calls it "likely nothing to
  worry about".
- `network_policy_block`. The agent blamed a pod restart that the benchmark
  injected as noise, and it never queried NetworkPolicies. Search its tool
  outputs for `[OOM]` to see `kubectl` calls that HolmesGPT's tool memory limit
  killed.
- The cost. Langfuse's total is 3 to 17 times the real cost, because HolmesGPT
  reports a running token total on every LLM span. The step prints both numbers.

Langfuse features: OpenTelemetry ingestion, environments, sessions, tags,
metadata, model prices.

## What should the agent have found?

Command: `make create-dataset`

To grade an answer, you need the right answer. SREGym injects each fault itself,
so it knows the true root cause. In the study, we loaded the 17 problems of
SREGym-Lite as a Langfuse dataset.

The step creates the dataset `sregym-lite`. Each item's input is what the agent is
told, which is the app, its namespaces and a description. Each item's expected
output is the root cause that SREGym injected, and the metadata holds SREGym's
settings for the problem.

Langfuse features: datasets, dataset items.

## How often is the agent right, and at what cost?

Command: `make experiment-runs`

In the study, the driver linked each investigation to its dataset item as part of
a named run. A script then turned SREGym's grades into Langfuse scores, next to
the real cost and effort of each investigation.

The step recreates both runs and imports the grades:
- The `diagnosis-*` scores hold SREGym's pass or fail, its score, and its
  localization, characterization and scope checks.
- The `holmes-*` scores hold the corrected cost, the tokens and the LLM calls.
- `time-to-diagnose-s` holds the time.

What to look at:
- The pass rate. The agent passed 3 of 5 problems with a 40-step limit.
- The compare view that the step links to. The case
  `duplicate_pvc_mounts_social_network` failed with a 40-step limit and passed
  with a 100-step limit.
- The effort. Failures used 19 to 40 LLM calls and cost $2.58 to $7.34, and passes
  used 7 to 13 calls and cost $0.35 to $1.51.

Langfuse features: dataset runs, run comparison, scores.

## Can an LLM judge in Langfuse grade the agent?

Command: `make offline-judges`

SREGym grades answers with its own LLM judge. A team that uses its own incidents
has no SREGym, so it needs a judge that runs in Langfuse. In the study, we judged
the same answers in three ways, all with SREGym's checklist of 9 questions:
- SREGym's own judge.
- A Langfuse managed evaluator named `sregym`. A managed evaluator only scores
  experiment runs created after its rule exists, so we replayed the agent's
  answers as new experiment runs with `replay_judge_check.py`.
- An SDK judge, `sdk_judge.py`, which runs `run_batched_evaluation` over the
  traces and combines the three checklist dimensions in a weighted composite score.

With a key, the step creates the LLM connection, the evaluator and its rule, and
then it runs both scripts. Without a key, it imports the replay runs and the saved
scores. Either way it prints the three judges side by side.

What to look at:
- The three judges agree on pass or fail for all 6 answers.
- Their scores for each checklist dimension differ. For example, on
  `env_variable_shadowing_astronomy_shop` SREGym gives 0.89, the managed evaluator
  0.78 and the SDK judge 1.00.

Langfuse features: LLM connections, managed evaluators, evaluation rules,
experiments through the SDK, `run_batched_evaluation`, composite evaluators.

## Why the agent failed

A grade says that the agent failed. The trace says why. The table lists each
failure we found, how to detect it, and where it shows in Langfuse.

| # | Failure | Why it happens | How to detect it | Where in Langfuse | What we saw |
|---|---|---|---|---|---|
| 1 | Distracted by noise | Injected pod kills look like causes | Read the trace, and the judges' scores | Trace view, `diagnosis-*` and `sdk-diagnosis-*` scores | On `network_policy_block`, the agent blamed a restart and never checked NetworkPolicies |
| 2 | Saw the evidence and dismissed it | The agent's reasoning wrote off an unhealthy pod | Read the LLM call's reasoning | LLM call output | On the PVC case, the agent called the Pending `jaeger` pod "likely nothing to worry about" |
| 3 | Tools killed | HolmesGPT's tool memory limit of 800 MB | Search the tool outputs for `[OOM]` | Trace view | `[OOM]` on plain `kubectl get pods` calls |
| 4 | Ran out of steps with no answer | A limit of 40 steps | `holmes_max_steps` in the trace metadata, and a rerun with 100 steps | Run comparison | The PVC case failed at 40 steps and passed at 100 steps, in 13 steps |
| 5 | Cost reported too high | The agent reports running token totals | Compare Langfuse's cost with `holmes-cost-usd` | Trace cost and scores | 3 to 17 times too high |
