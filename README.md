# Infrastructure operations agent

An OpenAI Agents SDK application operates a fictional local SQLite deployment CLI. It reads current status, starts the exact authorized rollout, checks health and can recover its own failed rollout. API2.1.0 deliberately fails health; worker1.5.0 succeeds. Real cloud accounts and credentials are outside this example.

## Run

Requires Python3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --extra test
uv run --locked infra-demo seed
uv run --locked infra-demo status api
```

Set an enabled `INFRA_MODEL` and `OPENAI_API_KEY`. These are required explicitly; no model is selected by the example. Inspect without write permission:

```sh
uv run --locked infra-agent --request 'Inspect the current API deployment and its history'
```

For a rollout, first obtain the current structured caller intent using the private model-free CLI:

```sh
uv run --locked infra-demo inspect --service api --version 2.1.0 --rollback-on-failure
uv run --locked infra-agent --allow-writes --intent-json '{"service":"api","version":"2.1.0","expected_revision":1,"rollback_on_failure":true}' --request 'Read fresh API status before each change. Deploy2.1.0, check health and recover this failed rollout if needed. Verify final status and history.'
```

Use the actual current revision from inspection. `--db PATH` selects the local SQLite source, `--output DIR` selects private run receipts and `--timeout-seconds` sets a shared1..300second budget (library minimum0.01). `--base-url` accepts an exact HTTPS `/v1` endpoint or an explicitly owned loopback HTTP provider. A real model request can incur provider cost; the repository's tests use only owned local HTTP and synthetic keys.

## Operation contract

The five SDK tools run fixed real Python CLI subprocesses with private bounded stdin and a minimal environment. They expose status/history, rollout, health and recovery, with no shell or arbitrary cloud command. Generic write flags do not grant tool authority: writes require the exact caller service/version/initial revision/recovery permission, a genuine fresh same-operation current read and this operation's own transitions. Cached snapshots and private intent inspection grant no runtime read. CLI/source methods used manually are an explicitly trusted synthetic-human fixture seam.

The source retains original fixture bytes and complete version/health rules, event history, cached states, observations and atomic decisions. Managed UUID, root/file identity and exact SQLite schema are checked before/after effects. Missing, copied, replaced, foreign, contradictory or modified source is rejected without repair. Existing legacy databases require a fresh exclusive seed. Keep the original database and receipts for inspection; do not delete evidence to reset a workflow.

Original complete Responses JSON, item statuses, call IDs and strict arguments are checked before SDK tool dispatch. Explicit per-run model/key/origin, no retries,14requests,13CLIcommands,4096output tokens, bounded streams and a shared deadline are enforced. Completion requires a fresh final source observation; deploy-start and final prose alone do not establish a healthy deployment. Failed rollout with verified recovery, successful rollout, unchanged state and unresolved partial state are separate outcomes.

Private `artifacts/<run UUID>/trace.json` receipts retain source images, original request/response bytes, unknown fields, SDK normalization, real child PIDs/stdout/stderr/exits, earlier committed effects and cleanup. Secrets are excluded from request headers and private stdin evidence. Original reported token counters distinguish missing/invalid/zero from SDK estimates; actual cost and cloud delivery remain unknown. Receipts are sensitive local evidence, not an OS sandbox or billing guarantee. SIGINT/SIGTERM/SIGHUP and cancellation close durable write authority before draining owned work. Failed closure or recording prevents success and keeps partial outcomes explicit.

## Development status

```sh
uv run --locked --extra test pytest -q
```

94checks currently cover source/intent/read/transition authority, six real concurrent writers, strict original native output, actual SDK/HTTP/CLI recovery, genuinely paused-child fencing, signal/deadline/parent cancellation and failed source/recording/resource closure. [PLAN.md](PLAN.md) records baseline reproductions, exact evidence and remaining twelve-case/distribution tasks. These are offline execution proofs. Enabled live model/budget/semantic and billing review, qualified new Firedrill persisted scenarios, real cloud/account integration and publication/license remain parked. The project is still under independent development.

Explore the [Firedrill example catalog](https://github.com/firedrill-tools/firedrill-example-catalog). Product references: [Firedrill](https://firedrill.run/) and its [official documentation](https://docs.firedrill.run/); use the documentation matching a qualified release.
