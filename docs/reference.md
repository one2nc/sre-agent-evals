# Reference

## Contents

- [Example trace](#example-trace)
- [Span attributes](#span-attributes)
- [Data files](#data-files)
- [Folder layout](#folder-layout)
- [What the demo doesn't show](#what-the-demo-doesnt-show)

## Example trace

The table shows the first 14 of 27 spans in one investigation, of the problem
`service_wrong_pod_selection_hotel_reservation` in the run `holmes-noise-run-1`
(trace `a2f7d8f60136802a19ad6544624643c0`, 50 seconds).

| # | Span | Parent | Duration | Source | What it tells you | Sample attributes |
|---|---|---|---|---|---|---|
| 1 | `holmes ask "You are on call..."` | root | 50.2 s | HolmesGPT | One investigation, with the prompt as the trace input | `type=user_question` |
| 2 | `gen_ai.chat` (generation) | 1 | 3.7 s | HolmesGPT | LLM call 1, which reads the prompt and picks tools | `gen_ai.request.model=openrouter/anthropic/claude-sonnet-5.5`, `gen_ai.usage.input_tokens=14456` |
| 3 | `POST` | 2 | 1.6 s | httpx, automatic | The HTTP call to OpenRouter | `http.url=https://openrouter.ai/api/v1/chat/completions`, `http.status_code=200` |
| 4 | `kubernetes_tabular_query` | 1 | 2.9 s | HolmesGPT tool | Pod phases, readiness and restarts | `holmesgpt.tool.status=success` |
| 5 | `bash` | 1 | 5.3 s | HolmesGPT tool | `kubectl get events -n hotel-reservation ...` | `holmesgpt.tool.name=bash` |
| 6 | `gen_ai.chat` | 1 | 4.7 s | HolmesGPT | LLM call 2, with input tokens that grow with the transcript | `gen_ai.usage.input_tokens=33449` |
| 7 | `POST` | 6 | 2.5 s | httpx, automatic | The HTTP call | `http.status_code=200` |
| 8 | `bash` | 1 | 0.1 s | HolmesGPT tool | A piped command that the read-only policy rejected | `holmesgpt.tool.status=error` |
| 9 | `fetch_pod_logs` | 1 | 0.3 s | HolmesGPT tool | Logs of the `geo` pod | `description=Fetch Logs (pod=geo-...)` |
| 10 | `gen_ai.chat` | 1 | 4.8 s | HolmesGPT | LLM call 3 | `gen_ai.usage.input_tokens=56168` |
| 11 | `POST` | 10 | 1.2 s | httpx, automatic | The HTTP call | |
| 12 | `kubernetes_tabular_query` | 1 | 2.9 s | HolmesGPT tool | Services, with ports, target ports and selectors | |
| 13 | `kubernetes_tabular_query` | 1 | 2.8 s | HolmesGPT tool | Endpoints, which show the pods behind each service and hold the clue | |
| 14 | `fetch_pod_logs` | 1 | 0.6 s | HolmesGPT tool | Logs of the `reservation` pod | |

The driver adds more to each trace through the API. It sets the session (the run),
the tags (`problem:...`, `model:...` and `sregym`), the metadata (e.g.,
`holmes_max_steps` and `target_namespaces`) and the final answer as the trace
output.

## Span attributes

| Attribute | Set by | Example | Use |
|---|---|---|---|
| `gen_ai.request.model`, `gen_ai.system` | HolmesGPT | `openrouter/anthropic/claude-sonnet-5.5`, `litellm` | Marks the span as an LLM call, and selects the model price |
| `gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens` | HolmesGPT | `14456` | Tokens and cost. HolmesGPT reports a running total, so Langfuse over-counts the cost. |
| `holmesgpt.iteration` | HolmesGPT | `9` | The step number |
| `langfuse.observation.input`, `output` | HolmesGPT, with `HOLMES_LANGFUSE_ATTRIBUTES=true` | The messages as JSON, and `{"content": ...}` | The input and output in Langfuse. Langfuse reads tool calls from the output. |
| `langfuse.observation.level`, `status_message` | HolmesGPT | `ERROR` | Marks failed tool calls |
| `holmesgpt.tool.name`, `holmesgpt.tool.status`, `description`, `error`, `return_code` | HolmesGPT tools | `bash`, `error` | The command, and whether it worked |
| `http.method`, `http.url`, `http.status_code` | httpx, automatic | `POST`, `.../chat/completions`, `200` | The provider call under each LLM call |
| Resource `deployment.environment` | The driver | `sregym` | The Langfuse environment |
| Resource `sregym.problem_id`, `sregym.run_name` | The driver | `network_policy_block`, `holmes-noise-run-1` | The problem and the run |

## Data files

| File | Contents |
|---|---|
| `traces.json.gz` | 6 investigation traces and 6 evaluator replay traces, with 398 observations |
| `scores.json.gz` | 102 scores on those traces: SREGym's grades, the SDK judge and the `sregym` evaluator |
| `datasets.json` | `sregym-lite` with 17 items |
| `dataset_runs.json` | 4 runs and their items: the 2 agent runs and their 2 evaluator replays |
| `evaluators.json` | The `sregym` evaluator and its rule |
| `models.json` | The model price |
| `rca_checklists.yaml` | SREGym's root cause checklist, used by the SDK judge, under the MIT license in `rca_checklists.LICENSE.txt` |
| `manifest.json` | Counts and source versions, which are Langfuse 3.225.11, HolmesGPT `4efc18f` and SREGym `0d8e7b7` |

`export/export_demo_data.py` wrote the files through Langfuse's public API, and it
removed the source project's public key from the traces. The source project also
holds the study's online work, which the export leaves out. The traces hold only data
from the benchmark's own Kubernetes cluster, e.g., pod names, cluster IP addresses,
logs and events of the benchmark apps.

## Folder layout

```
data/                     exported study data
demo/
  lf.py                   settings, Langfuse API, OpenTelemetry replay, timestamps
  steps.py                the evaluation steps, run as `python -m demo <step>`
  sdk_judge.py            the study's scripts; sdk_judge.py reads the checklist
  replay_judge_check.py   from data/
docs/                     this documentation
tests/test_data.py        checks data/ against what the steps look up (make test)
export/export_demo_data.py
docker-compose.yml        overlay on Langfuse's compose file: image pins and the demo service
langfuse/                 gitignored; Langfuse's compose file, downloaded and checked by make
```

## What the demo doesn't show

- The agent running live. The investigations are recordings, and running
  HolmesGPT on SREGym needs a Kubernetes cluster, SREGym, HolmesGPT and an LLM key.
- How often results repeat. Each run tried each case once, so the results show a
  direction, not a rate.
- Online evaluation, which scores live investigations that have no root cause to
  compare against. A later version of the demo will add it.
- The managed evaluator scoring the investigations themselves. Langfuse's
  experiment evaluators skip traces that are linked to a run after they arrive,
  so the study replayed the answers as new SDK experiment runs.
