"""Hermes ACP host with project tools enforced independently of model output.

Uses the installed Hermes inference/session engine. No shell, native plugin,
MCP, browser, or nested-agent tool is dispatched by this host. Work mode
may run project commands with the operator account; it is not an OS sandbox.
"""
import argparse
import asyncio
import contextlib
import copy
import json
import logging
import selectors
import signal
import subprocess
import time
import os
from pathlib import Path
import stat
import sys
import uuid

MAX_BYTES = 1_000_000
COMMANDS = set()

def stop_commands(*_):
    for process in list(COMMANDS):
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if _: raise KeyboardInterrupt

BLOCKED = {'.git', '.botspace', '.hermes', '.codex', '.claude', 'node_modules', '__pycache__'}


class ProjectTools:
    def __init__(self, directory, mode, state_directory=None, readable=None):
        if mode not in ('read', 'work'):
            raise ValueError('Choose read or work mode')
        self.root = Path(directory).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Project directory must exist')
        self.mode = mode
        self.readable_path = Path(os.path.abspath(readable)) if readable else None
        self.readable = None
        self.state = Path(state_directory).resolve() if state_directory else None
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _parts(self, value):
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('Supply a project-relative path')
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.root)
            except ValueError:
                raise ValueError('Path is outside the project') from None
        parts = path.parts
        if not parts or '..' in parts or any(p in BLOCKED or p == '.env' or p.startswith('.env.') for p in parts):
            raise ValueError('Path is outside the allowed project files')
        if self.state and (self.root.joinpath(*parts) == self.state or self.state in self.root.joinpath(*parts).parents):
            raise ValueError('Runner state is private')
        return parts

    @contextlib.contextmanager
    def _parent(self, value, create=False):
        parts = self._parts(value)
        descriptor = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def read(self, path):
        if self.readable_path and isinstance(path, str) and Path(path).is_absolute():
            try: Path(path).relative_to(self.root)
            except ValueError:
                if self.readable_path.is_symlink() or not self.readable_path.is_dir():
                    raise ValueError('Additional readable project directory is unavailable')
                if self.readable is None:
                    self.readable = ProjectTools(self.readable_path, 'read', self.state)
                return self.readable.read(path)
        with self._parent(path) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError('Read a regular text file of at most 1 MB')
                with os.fdopen(descriptor, 'r', encoding='utf-8', closefd=False) as stream:
                    return stream.read(MAX_BYTES)
            finally:
                os.close(descriptor)

    def write(self, path, content):
        if self.mode != 'work':
            raise ValueError('Read mode does not permit edits')
        if not isinstance(content, str) or len(content.encode()) > MAX_BYTES:
            raise ValueError('Write at most 1 MB of text')
        with self._parent(path, create=True) as (parent, name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Refusing a symlink, special file, or hard-linked target')
            except FileNotFoundError:
                pass
            temporary = '.truffle-write-' + uuid.uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(content)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {'written': path, 'bytes': len(content.encode())}

    def search(self, query=''):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError('Search query must be at most 500 characters')
        results, examined = [], 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in BLOCKED and not Path(directory, d).is_symlink())
            for name in sorted(files):
                examined += 1
                if examined > 2000 or len(results) >= 100:
                    return {'matches': results, 'truncated': True}
                path = str(Path(directory, name).relative_to(self.root))
                try:
                    self._parts(path)
                    if not query:
                        results.append({'path': path})
                        continue
                    for index, line in enumerate(self.read(path).splitlines()):
                        if query.casefold() in line.casefold():
                            results.append({'path': path, 'line': index + 1, 'text': line[:500]})
                            if len(results) >= 100:
                                return {'matches': results, 'truncated': True}
                except (OSError, ValueError, UnicodeError):
                    continue
        return {'matches': results, 'truncated': False}

    def run(self, command, timeout=30):
        if self.mode != 'work':
            raise ValueError('Read mode does not permit commands')
        if not isinstance(command, str) or not command.strip() or len(command) > 10000:
            raise ValueError('Supply a project command of at most 10000 characters')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError('Command timeout must be between 1 and 60 seconds')
        process = subprocess.Popen(['/bin/sh', '-c', command], cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
        COMMANDS.add(process)
        output, total, timed_out = bytearray(), 0, False
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    output.extend(chunk[:max(0, 65536-len(output))])
            code = process.wait(timeout=2)
        finally:
            selector.close()
            process.stdout.close()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
            COMMANDS.discard(process)
        return {'output': output.decode('utf-8', errors='replace'), 'exit_code': code,
                'timed_out': timed_out, 'truncated': total > 65536}

    def dispatch(self, name, arguments, *args, **kwargs):
        try:
            if not isinstance(arguments, dict):
                raise ValueError('Tool arguments must be an object')
            if name == 'project_read':
                result = {'text': self.read(arguments.get('path'))}
            elif name == 'project_search':
                result = self.search(arguments.get('query', ''))
            elif name == 'project_run' and self.mode == 'work':
                result = self.run(arguments.get('command'), arguments.get('timeout', 30))
            elif name == 'project_write' and self.mode == 'work':
                result = self.write(arguments.get('path'), arguments.get('content'))
            elif name == 'project_patch' and self.mode == 'work':
                old, new = arguments.get('old'), arguments.get('new')
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise ValueError('Supply nonempty old text and replacement new text')
                text = self.read(arguments.get('path'))
                if text.count(old) != 1:
                    raise ValueError('Old text must match exactly once; reread the file')
                result = self.write(arguments['path'], text.replace(old, new, 1))
            else:
                raise ValueError('Tool is not authorized in this Truffle mode')
            return json.dumps(result)
        except (OSError, ValueError, UnicodeError) as error:
            return json.dumps({'error': str(error)})

    def definitions(self):
        definitions = [
            ('project_read', 'Read a UTF-8 file inside the project. Private state and symlinks are blocked.', {'path': {'type': 'string'}}, ['path']),
            ('project_search', 'List project files when query is empty, or search text. Returns bounded results.', {'query': {'type': 'string'}}, []),
        ]
        if self.mode == 'work':
            definitions += [
                ('project_run', 'Run a project command or tests as the operator, with cwd fixed to this project. Work mode only; this is not an OS sandbox. Timeout <=60 seconds; output bounded.', {'command': {'type': 'string'}, 'timeout': {'type': 'number'}}, ['command']),
                ('project_write', 'Create or replace a UTF-8 project file. Private state and symlinks are blocked.', {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
                ('project_patch', 'Replace one exact text occurrence in a project file. Reread on mismatch.', {'path': {'type': 'string'}, 'old': {'type': 'string'}, 'new': {'type': 'string'}}, ['path', 'old', 'new']),
            ]
        return [{'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}} for name, description, properties, required in definitions]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['read', 'work'], required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--readable')
    args = parser.parse_args()
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop_commands)
    signal.signal(signal.SIGINT, stop_commands)
    project = ProjectTools(args.directory, args.mode, state, args.readable)
    original_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).resolve()
    # Resolve account settings before isolating runtime state; credentials stay in
    # memory and are never written into the Truffle state directory.
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=original_home)
    from hermes_cli.config import load_config
    config = load_config()
    model_config = config.get('model') or {}
    default_model = model_config.get('default', '') if isinstance(model_config, dict) else str(model_config)
    provider = model_config.get('provider') if isinstance(model_config, dict) else None
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested=provider)
    if runtime.get('command') or runtime.get('args'):
        raise ValueError('Choose a Hermes API provider for this isolated host; nested runtime providers cannot enforce the project tool boundary')
    os.environ['HERMES_HOME'] = str(state / 'home')
    Path(os.environ['HERMES_HOME']).mkdir(mode=0o700, exist_ok=True)
    # No user hooks, MCP servers, plugins, memories, or command-based context
    # engines enter this dedicated host. Provider settings were resolved above.
    import hermes_cli.config as hermes_config
    hermes_config.load_config = lambda *a, **kw: {}
    logging.disable(logging.CRITICAL)
    import run_agent
    import model_tools
    definitions = project.definitions()
    run_agent.get_tool_definitions = lambda *a, **kw: copy.deepcopy(definitions)
    model_tools.get_tool_definitions = run_agent.get_tool_definitions
    run_agent.handle_function_call = project.dispatch
    model_tools.handle_function_call = project.dispatch
    import acp
    from acp_adapter.session import SessionManager, _register_task_cwd
    from acp_adapter.server import HermesACPAgent
    from hermes_state import SessionDB
    db = SessionDB(db_path=state / 'state.db')

    class ProjectSessions(SessionManager):
        def _make_agent(self, *, session_id, cwd, model=None, **kwargs):
            if Path(cwd).resolve() != project.root:
                raise ValueError('Session belongs to a different project')
            _register_task_cwd(session_id, str(project.root))
            # quiet_mode prevents provider initialization from displaying even
            # partial credential values. No transcript/debug log is enabled.
            agent = run_agent.AIAgent(
                model=model or default_model, provider=runtime.get('provider'),
                api_mode=runtime.get('api_mode'), base_url=runtime.get('base_url'),
                api_key=runtime.get('api_key'), session_id=session_id, session_db=db,
                platform='acp', enabled_toolsets=['truffle-project'],
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                load_soul_identity=False, save_trajectories=False, verbose_logging=False,
                checkpoints_enabled=False, max_iterations=30,
                ephemeral_system_prompt='You are Hermes connected through Truffle. Use only the provided project tools. ' +
                    ('Review without edits. ' if args.mode == 'read' else 'You may read and edit this project. ') +
                    ('Shell execution is unavailable in read mode. ' if args.mode == 'read' else 'Use project_run for project commands and tests; commands use the operator account and are not OS-sandboxed. ') +
                    'Return requested managed peer actions as text JSON; do not invoke a nested-agent tool.',
            )
            agent.tools = copy.deepcopy(definitions)
            agent.valid_tool_names = {tool['function']['name'] for tool in definitions}
            agent._print_fn = lambda *a, **kw: None
            return agent

    class ProjectHost(HermesACPAgent):
        async def _register_session_mcp_servers(self, state, mcp_servers):
            if mcp_servers:
                raise ValueError('External tools are not permitted in the project host')

    asyncio.run(acp.run_agent(ProjectHost(ProjectSessions(db=db)), use_unstable_protocol=True))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        # Raw provider errors can contain URLs or authentication material.
        print('Hermes host could not complete startup. Check Hermes login and its ACP installation.', file=sys.stderr)
        sys.exit(1)
