import { access, realpath, readFile, mkdir } from 'node:fs/promises';
import { constants } from 'node:fs';
import { resolve, join, dirname, delimiter } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

export async function hermesPython(env = process.env) {
  if (env.HERMES_PYTHON) { await access(env.HERMES_PYTHON, constants.X_OK); return env.HERMES_PYTHON; }
  const candidates = [];
  for (const path of (env.PATH || '').split(delimiter)) {
    const command = join(path, 'hermes');
    try {
      const target = await realpath(command);
      const header = (await readFile(target, 'utf8')).slice(0, 4096);
      const shebang = header.match(/^#!(\S*python\S*)/);
      if (shebang) candidates.push(shebang[1]);
      const launcher = header.match(/exec\s+["']([^"']+\/bin\/hermes)["']/);
      if (launcher) candidates.push(join(dirname(launcher[1]), 'python'));
      candidates.push(join(dirname(target), 'python'));
      break;
    } catch {}
  }
  const roots = [env.HERMES_PYTHON_SRC_ROOT, join(homedir(), '.hermes', 'hermes-agent'), env.HERMES_HOME && join(env.HERMES_HOME, 'hermes-agent')].filter(Boolean);
  for (const root of roots) for (const venv of ['venv', '.venv']) candidates.push(join(root, venv, 'bin', 'python'));
  for (const candidate of [...new Set(candidates.filter(Boolean))]) {
    try { await access(candidate, constants.X_OK); return candidate; } catch {}
  }
  throw Error('Hermes Python environment not found. Install Hermes with its ACP extra, or set HERMES_PYTHON to that environment’s Python executable.');
}

export async function main(args = process.argv.slice(2)) {
  const options = {};
  for (let i=0;i<args.length;i+=2) {
    if (!['--mode','--state','--readable'].includes(args[i]) || !args[i+1] || options[args[i]]) throw Error('Invalid Hermes host options');
    options[args[i]]=args[i+1];
  }
  if (!['read','work'].includes(options['--mode'])) throw Error('Choose --mode read or work.');
  const directory = await realpath(process.cwd());
  const state = resolve(options['--state'] || process.env.TRUFFLE_HERMES_STATE || join(process.env.KANBOT_HOME || process.env.BOTSPACE_DIR || join(directory, '.botspace'), 'hermes-runtime'));
  await mkdir(state, { recursive: true, mode: 0o700 });
  const child = spawn(await hermesPython(), [fileURLToPath(new URL('./hermes-host.py', import.meta.url)), '--mode', options['--mode'], '--directory', directory, '--state', state, ...(options['--readable'] ? ['--readable', options['--readable']] : [])], {stdio:'inherit', env:{...process.env, PYTHONDONTWRITEBYTECODE:'1'}});
  const stop = () => child.kill('SIGTERM');
  process.once('SIGINT', stop); process.once('SIGTERM', stop);
  await new Promise((done, reject) => { child.once('error', reject); child.once('close', code => { process.exitCode = code || 0; done(); }); });
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(() => { console.error('Hermes runtime unavailable. Check the Hermes Python installation and ACP dependencies.'); process.exitCode=1; });
