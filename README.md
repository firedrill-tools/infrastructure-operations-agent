# Infrastructure operations agent with OpenAI Agents SDK

An OpenAI Agents SDK application operates a fictional local SQLite deployment CLI. It reads current status, starts the exact authorized rollout, checks health and can recover its own failed rollout. API2.1.0 deliberately fails health; worker1.5.0 succeeds. Real cloud accounts and credentials are outside this example.

Simulate this OpenAI Agents SDK AI agent with [Firedrill](https://firedrill.run/), a simulation platform for AI agents. Start with the [Firedrill setup and simulation guide](firedrill/README.md); your coding agent can operate it while this application's runner stays in your repository or CI.

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

Use the actual current revision from inspection. `--db PATH` selects the local SQLite source, `--output DIR` selects private run receipts and `--timeout-seconds` sets a shared0.01..300second budget. `--base-url` accepts an exact HTTPS `/v1` endpoint or an explicitly owned loopback HTTP provider. A real model request can incur provider cost; the repository's tests use only owned local HTTP and synthetic keys.

## Operation contract

The five SDK tools run fixed real Python CLI subprocesses with private bounded stdin and a minimal environment. They expose status/history, rollout, health and recovery, with no shell or arbitrary cloud command. Generic write flags do not grant tool authority: writes require the exact caller service/version/initial revision/recovery permission, a genuine fresh same-operation current read and this operation's own transitions. Cached snapshots and private intent inspection grant no runtime read. CLI/source methods used manually are an explicitly trusted synthetic-human fixture seam.

The source retains original fixture bytes and complete version/health rules, event history, cached states, observations and atomic decisions. Managed UUID, root/file identity and exact SQLite schema are checked before/after effects. Missing, copied, replaced, foreign, contradictory or modified source is rejected without repair. Existing legacy databases require a fresh exclusive seed. Keep the original database and receipts for inspection; do not delete evidence to reset a workflow.

Original complete Responses JSON, item statuses, call IDs and strict arguments are checked before SDK tool dispatch. Explicit per-run model/key/origin, no retries,14requests,13CLIcommands,4096output tokens, bounded streams and a shared deadline are enforced. Completion requires a fresh final source observation; deploy-start and final prose alone do not establish a healthy deployment. Failed rollout with verified recovery, successful rollout, unchanged state and unresolved partial state are separate outcomes.

Private `artifacts/<run UUID>/trace.json` receipts retain source images, original request/response bytes, unknown fields, SDK normalization, real child PIDs/stdout/stderr/exits, earlier committed effects and cleanup. Secrets are excluded from request headers and private stdin evidence. Original reported token counters distinguish missing/invalid/zero from SDK estimates; actual cost and cloud delivery remain unknown. Receipts are sensitive local evidence, not an OS sandbox or billing guarantee. SIGINT/SIGTERM/SIGHUP and cancellation close durable write authority before draining owned work. Failed closure or recording prevents success and keeps partial outcomes explicit.

## Twelve private deployment cases

```sh
uv run --locked infra-cases list
uv run --locked infra-cases init --case starter_failed_recovery --root workspace/api-case
uv run --locked infra-cases inspect --root workspace/api-case
uv run --locked infra-cases reset --root workspace/api-case --new-root workspace/api-case-fresh
```

Initialization is exclusive. Inspection prints the selected DB path, current structured caller intent and private case reference. Use that DB and intent with `infra-agent`; inspection grants no runtime status capability. Reset validates the old source and creates a separate fresh root/UUID/seed, preserving all old state and receipts. Existing/missing/foreign/copied/corrupt/stripped authority and a held private control mutex are rejected without repair. Failed initialization directories remain available for diagnosis.

The pinned dataset has three starter, three busy, three ambiguous and three boundary cases. Busy data contains24services,240allowedversions and40–43real prior rollout/health/recovery events. Pending or failed historical work belongs to its earlier author. Unicode descriptions and literal hostile/file mentions are untrusted business text. Historical model/authentication/cloud delivery are unknown. [scenarios/README.md](scenarios/README.md) lists case outcomes.

## Development status

```sh
uv run --locked --extra test pytest -q
```

130checks currently cover source/intent/read/transition authority, six real concurrent writers, strict original native output, actual SDK/HTTP/CLI recovery, genuinely paused-child fencing, signal/deadline/parent cancellation and failed source/recording/resource closure. [PLAN.md](PLAN.md) records baseline reproductions, exact evidence and the complete distribution qualification contract. These are offline execution proofs. Enabled live model/budget/semantic and billing review, authenticated Firedrill persistence verification, real cloud/account integration and publication/license remain parked. Independent completion additionally requires the exact pushed Linux distribution gate and downloaded artifact equality recorded in the portfolio catalog.

Explore the [Firedrill example catalog](https://github.com/firedrill-tools/firedrill-example-catalog). Product references: [Firedrill](https://firedrill.run/) and its [official documentation](https://docs.firedrill.run/); use the documentation matching a qualified release.

## Reproduce distribution qualification

```sh
uv run --locked python scripts/build_populations.py --check
uv build
uv run --locked python scripts/check_distribution.py
```

The gate builds a deterministic complete source archive using portable stored DEFLATE with file/mode/size/hash manifest, repeats its bytes and exercises locked wheel, sdist, complete source and separate production-only installs outside the checkout. Each installation runs all12actual private case CLIs/reset, the installed normal agent through owned local HTTP/real CLI recovery, prepared missing-key failure, all three signals and final native failure after actual committed recovery. The complete source also runs the entire130check suite. Delivered source hashes remain unchanged. `dist/qualification.json` records the observed gate; CI uploads it, the fresh suite log and original archives. `COMPATIBILITY.md` records the exact proof limits.

## Current Firedrill integration

Released CLI/SDK 2.2.2, current command invocation, remote operator MCP and recovery are documented in [firedrill/README.md](firedrill/README.md). Offline checks preserve actual private case/native key behavior and existing independent authority. Authenticated persisted runs and live quality/cost remain separate parked gates.
