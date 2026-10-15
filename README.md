# Infrastructure operations agent

An OpenAI Agents SDK agent operates a local synthetic deployment CLI. It reads fresh service status, starts a rollout, checks health, and rolls back a failed version using the latest revision. The fixture deliberately makes API version 2.1.0 fail health. Cached status can be stale, while the worker's 1.5.0 rollout succeeds.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra test
uv run infra-demo seed
uv run infra-demo status api
```

Set `OPENAI_API_KEY` and an enabled `INFRA_MODEL`, then inspect without allowing changes:

```sh
uv run infra-agent --request "Inspect the current API deployment and explain what a 2.1.0 rollout would require"
```

To allow a synthetic rollout and recovery:

```sh
uv run infra-agent --request "Deploy API version 2.1.0, check health, and if it fails roll back to the previous healthy version. Verify the final state." --allow-writes
```

`--db` changes the local SQLite path; by default it is `workspace/deployments.db`. The SDK agent's tools invoke the real `infra-demo` subprocess with fixed command shapes. The CLI supports `status`, `deploy`, `health`, `rollback` and `history`. `status --cached` deliberately returns an old snapshot until a fresh status read updates it. Mutating commands require the current revision; stale requests fail. The agent write gate covers rollout, health and rollback tools. The final JSON includes SDK usage when available and the committed service state.

Only the synthetic versions in `fixtures/starter.json` are accepted. The tool does not load cloud credentials or call a cloud API. Reset by removing the local `workspace` directory and running `uv run infra-demo seed` again. This SQLite fixture is a local example, not a production deployment system.

`uv run --extra test pytest` covers a failed rollout, stale cache, rollback, successful worker rollout, CLI subprocess calls and actual Agents SDK function tools. These are offline tool and state proofs, not a live OpenAI model run. Live provider and new Firedrill integration proofs remain pending. This private repository has no license pending portfolio publication decisions.
