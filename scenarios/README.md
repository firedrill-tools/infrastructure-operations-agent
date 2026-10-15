# Scenarios

Start each case from a freshly seeded database. Check final SQLite state and history, not only the agent response.

- **Failed rollout:** deploy API 2.1.0, observe degraded health, roll back to 2.0.0 and confirm healthy status.
- **Stale status:** read the cached API revision after a rollout; mutation with that revision must fail. Re-read fresh status before the next action.
- **Successful rollout:** deploy worker 1.5.0, check health and confirm it remains healthy without a rollback.
- **Invalid rollback:** refuse rollback when there is no failed rollout or the revision changed.
- **Read-only:** without `--allow-writes`, Agents SDK tools can inspect but cannot deploy, complete health checks or roll back.
