# Agent notes

Keep all operations on the synthetic local SQLite deployment CLI. Never add real cloud-admin credentials or broad shell execution. Preserve fresh status and revision checks, explicit write authorization, health verification and rollback from a degraded rollout only. Do not call a rollout complete from a deploy-start result. Keep fixtures synthetic and do not commit local database or credentials. Offline SDK tool invocation is not a live model or Firedrill proof.
