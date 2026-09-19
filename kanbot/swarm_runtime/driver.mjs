// Kanbot owns this process's PTY; native runtime protocols use clean pipes.
import { readFile, writeFile, rename } from 'node:fs/promises';
import { runRuntime } from './runtimes.mjs';

const file = process.argv[2];
const options = JSON.parse(await readFile(file, 'utf8'));
const abort = new AbortController();
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'])
  process.on(signal, () => abort.abort());
const timer = setTimeout(() => abort.abort(), options.timeout * 1000);
const save = async (suffix, data) => {
  const target = file + suffix;
  await writeFile(target + '.tmp', JSON.stringify(data), {mode: 0o600});
  await rename(target + '.tmp', target);
};
try {
  const result = await runRuntime({
    ...options, signal: abort.signal,
    onSession: (session) => save('.session', {session}),
  });
  await save('.result', {ok: true, ...result});
  console.log('Swarm turn completed.');
} catch (error) {
  await save('.result', {ok: false, error: error.message});
  console.error('Swarm turn stopped:', error.message);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
}
