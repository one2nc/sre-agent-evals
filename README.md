# Evaluating an AI SRE agent with Langfuse

We ran HolmesGPT, an AI SRE agent, on SREGym, a benchmark that breaks Kubernetes
apps and knows the true root cause of each fault. Langfuse recorded what the
agent did and scored each answer against the true root cause.

This demo replays that evaluation into a local Langfuse. You don't need a
cluster, the agent, or an LLM key: the recorded data is in `data/`. It covers
offline evaluation only, which grades answers against a known root cause.

## Run it

You need Docker with Compose 2.24 or later, and about 4 GB of free memory.

```bash
cp .env.example .env
```

Optional: set `OPENROUTER_API_KEY` in `.env`. With the key, `make offline-judges`
calls the judge models again, which costs a few US dollars. Without it, the demo
imports the study's saved judge scores. [LLM key](docs/setup.md#llm-key) has the
details.

Start Langfuse and leave it running:

```bash
make up
```

In a second shell, load the evaluation:

```bash
make all
```

Open http://localhost:3000 and sign in as `demo@example.com` with the password
`demo-password`.

To see what each step adds, run the steps one at a time instead of `make all`.
`make help` lists them, and [docs/evaluation.md](docs/evaluation.md) explains
each one.

## Commands

| Command | What it does |
|---|---|
| `make up` | Starts Langfuse |
| `make down` | Stops Langfuse |
| `make reset` | Stops Langfuse and deletes its data |
| `make all` | Runs every evaluation step |
| `make status` | Shows which steps have run |
| `make help` | Lists the steps |
| `make test` | Checks the files in `data/` |

## More documentation

- [docs/evaluation.md](docs/evaluation.md): each step of the evaluation, and why
  the agent failed.
- [docs/setup.md](docs/setup.md): ports, settings, the optional LLM key, and how
  to use your own Langfuse.
- [docs/reference.md](docs/reference.md): an example trace, the span attributes,
  the data files, and the folder layout.
