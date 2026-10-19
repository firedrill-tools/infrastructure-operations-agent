import { fileURLToPath } from 'node:url';
import { resolve, join } from 'node:path';
import { ApplicationError, commandMain, invokeCLI, privateDirectory } from './adapter-runtime.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
const cases = new Set(["starter_failed_recovery", "starter_healthy_worker", "starter_existing_target", "busy_mixed_history", "busy_pending_foreign", "busy_prior_recovery", "ambiguous_service_names", "ambiguous_stale_cache", "ambiguous_failed_unowned", "boundary_revision_exhausted", "boundary_last_health_revision", "boundary_unicode_literal"]);
export async function runApplication(input, parameters, signal, invocation) {
  if (Object.keys(input).some(key => !['caseId', 'action', 'task', 'allowWrites', 'intent'].includes(key)) || !cases.has(input.caseId) || !['run', 'inspect'].includes(input.action) ||
      typeof input.task !== 'string' || !input.task.trim() || input.task.length > 8000 || input.task.includes('\0') || typeof input.allowWrites !== 'boolean' ||
      (input.allowWrites ? !input.intent || typeof input.intent !== 'object' || Array.isArray(input.intent) || JSON.stringify(input.intent).length > 8000 : input.intent !== null) ||
      (input.action === 'inspect' && input.allowWrites)) throw new Error('Select a reviewed case/task/action and independent write grant with exact caller intent, or null for read-only.');
  const directory = await privateDirectory(root, invocation), workspace = join(directory, 'workspace');
  const cli = ['uv', 'run', '--locked', '--no-sync'];
  try {
    const initialized = await invokeCLI([...cli, 'infra-cases', 'init', '--root', workspace, '--case', input.caseId], { cwd: root, signal });
    const db = initialized.database;
    if (typeof db !== 'string' || !db.startsWith(workspace + '/')) throw new Error('Initialized database is outside its owned workspace.');
    const argv = input.action === 'inspect' ? [...cli, 'infra-cases', 'inspect', '--root', workspace] : [...cli, 'infra-agent', '--db', db, '--request', input.task, '--output', join(directory, 'receipts')];
    if (input.action === 'run') {
      if (input.allowWrites) argv.push('--allow-writes', '--intent-json', JSON.stringify(input.intent));
      if (parameters.model) argv.push('--model', parameters.model);
    }
    return { executionKind: input.action === 'inspect' ? 'application_state_inspection' : 'native_owned_cli_agent',
      application: await invokeCLI(argv, { cwd: root, signal }), localState: { directory, workspaceRoot: workspace, databaseFile: db } };
  } catch (error) { if (error instanceof ApplicationError) error.evidence.runDirectory = directory; throw error; }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await commandMain(runApplication);
