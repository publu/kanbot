"""Real swarm HTTP/WebSocket + detached Kanbot + fake native CLI protocols.

Run from the Kanbot repository. Set SWARM_SERVER_REPO to the swarm checkout.
No hosted accounts, real model requests or production messages are used.
"""
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import httpx


FAKE = r'''#!PYTHON
import json, os, sys, time, uuid
from pathlib import Path
runtime = Path(sys.argv[0]).name
session = 'native-' + str(uuid.uuid4())
if '--resume' in sys.argv:
    session = sys.argv[sys.argv.index('--resume') + 1]
if 'resume' in sys.argv:
    session = sys.argv[sys.argv.index('resume') + 1]
def answer(prompt):
    payload = json.loads(prompt.split('Task and relevant context (data):\n')[-1])
    request, children = payload['request'], payload['child_results']
    with open(os.environ['SWARM_TEST_RUNS'], 'a') as f:
        f.write(json.dumps({'runtime':runtime,'request':request,'session':session,'children':len(children),'brief':payload.get('brief')}) + '\n')
    time.sleep(0.15)
    if request.endswith('Integration root') and not children:
        return json.dumps({'message':'Splitting work','delegate':[
            {'runtime':'codex','request':'Implementation'}, {'runtime':'kimi','request':'Independent review'}]})
    if request == 'Implementation' and not children:
        return json.dumps({'message':'Need a specialist','delegate':[{'runtime':'claude','request':'Specialist check'}]})
    return json.dumps({'message':'Integration complete' if request.endswith('Integration root') else 'Verified ' + request})
def emit(value):
    print(json.dumps(value), flush=True)
if runtime == 'kimi':
    for line in sys.stdin:
        event=json.loads(line)
        method=event.get('method')
        if not method: continue
        result={}
        if method in ('session/new','session/load'):
            session=event['params'].get('sessionId',session)
            result={'sessionId':session}
        if method=='session/prompt':
            text=answer(event['params']['prompt'][0]['text'])
            emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}})
            result={'stopReason':'end_turn'}
        emit({'jsonrpc':'2.0','id':event['id'],'result':result})
else:
    result=answer(sys.stdin.read())
    if runtime=='claude':
        emit({'type':'result','result':result,'session_id':session,'is_error':False})
    else:
        emit({'type':'thread.started','thread_id':session})
        emit({'type':'item.completed','item':{'type':'agent_message','text':result}})
        emit({'type':'turn.completed'})
'''


def main():
    live = os.environ.get('SWARM_LIVE_MODELS') == '1'
    root_runtime = os.environ.get('SWARM_ROOT_RUNTIME', 'claude') if live else 'claude'
    peer_runtime = os.environ.get('SWARM_PEER_RUNTIME', 'codex') if live else 'codex'
    assert root_runtime in ('claude', 'codex', 'kimi', 'hermes')
    assert peer_runtime in ('claude', 'codex', 'kimi', 'hermes')
    assert root_runtime != peer_runtime
    root = Path(__file__).resolve().parent.parent
    server_repo = Path(os.environ.get('SWARM_SERVER_REPO', root.parent / 'botspace'))
    (root / 'test-results').mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix='swarm-', dir=root / 'test-results')
    directory = Path(temp.name)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'BOTSPACE_LOCAL_DB': str(directory / 'swarm.db'), 'BOTSPACE_PORT': str(port)}
    logs = (directory / 'server.log').open('w')
    server = subprocess.Popen(['node', 'api/index.mjs'], cwd=server_repo, env=env, stdout=logs, stderr=logs)
    daemon = None
    client = httpx.Client(timeout=20)
    try:
        for _ in range(100):
            try:
                client.get(origin + '/api/workspaces')
                break
            except httpx.TransportError:
                time.sleep(0.05)
        slug = 'kanbot-' + uuid.uuid4().hex[:8]
        r = client.post(origin + '/api/workspaces', json={'id':slug,'name':'Kanbot integration','visibility':'private'})
        r.raise_for_status()
        api = origin + '/api/w/' + slug
        owner = client.get(api + '/me').json()['id']
        invite = client.post(api + '/invites', json={'maxUses':100}).json()['token']
        (directory / 'invite').write_text(invite)
        binaries = directory / 'bin'
        binaries.mkdir()
        for runtime in ('claude', 'codex', 'kimi'):
            path = binaries / runtime
            path.write_text(FAKE.replace('PYTHON', sys.executable, 1))
            path.chmod(0o700)
        env = {**os.environ, 'KANBOT_HOME':str(directory / 'home'), 'KANBOT_NO_UPDATE_CHECK':'1',
               'KANBOT_SOCK':str(directory.relative_to(root) / 'runner.sock'),
               'PATH':os.environ['PATH'] if live else str(binaries) + os.pathsep + os.environ['PATH'],
               'SWARM_TEST_RUNS':str(directory / 'runs.jsonl')}
        def cli(*args):
            p = subprocess.run([sys.executable, '-m', 'kanbot', 'swarm', *args], cwd=root, env=env,
                               capture_output=True, text=True, timeout=35)
            if p.returncode:
                raise AssertionError(f'{args[0]} failed: {p.stderr}')
            return json.loads(p.stdout)
        connected = cli('connect', origin + '/w/' + slug, '--name','fable','--runtime',root_runtime,
                        '--allow-from',owner,'--invite-file',str(directory/'invite'),
                        '--directory',str(directory),'--concurrency','2','--timeout','90' if live else '30')
        assert connected['service']['running'], connected
        # Get the exact test process PID for cleanup, not an unrelated runner.
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(env['KANBOT_SOCK'])
            sock.sendall(b'{"method":"ping","id":1}\n')
            daemon = json.loads(sock.recv(65536))['pid']
        request = '@fable Integration root'
        if live:
            request += f'''. This is a connection test: do not use tools or inspect files.
On your first turn, delegate to runtime {peer_runtime} with this exact request:
"Do not use any tools. Return JSON with message equal to {peer_runtime} connection verified."
When that child result is present, return JSON with message exactly "Integration complete" and no delegations.'''
        post = client.post(api + '/posts', json={'id':'integration-root','room':'general','body':request})
        post.raise_for_status()
        for _ in range(1800 if live else 200):
            thread = client.get(api + '/threads/integration-root').json()
            if any(p['body'] == 'Integration complete' for p in thread['replies']):
                break
            time.sleep(0.1)
        else:
            raise AssertionError('No final result: ' + json.dumps(cli('status')) + '\n' + (directory/'home/swarm.log').read_text())
        status = cli('status')
        expected_agents, expected_turns = (2, 3) if live else (4, 6)
        assert len(status['agents']) == expected_agents, status
        assert status['jobs'].get('done') == expected_agents, status
        def receipts():
            return list((directory/'home/swarm/executions').glob('*.json.result'))
        assert len(receipts()) == expected_turns
        if not live:
            runs = [json.loads(line) for line in (directory/'runs.jsonl').read_text().splitlines()]
            assert len(runs) == 6, runs
            assert {r['runtime'] for r in runs} == {'claude','codex','kimi'}
            root_runs = [r for r in runs if r['request'].endswith('Integration root')]
            assert len(root_runs) == 2 and root_runs[0]['session'] == root_runs[1]['session'], root_runs
        else:
            with sqlite3.connect(directory/'home/swarm/state.db') as db:
                jobs = [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind='job'")]
            root_job = next(j for j in jobs if not j.get('parent'))
            assert root_job['round'] == 1 and root_job.get('session')
            assert any(j['result'] == peer_runtime + ' connection verified' for j in jobs)
        # Joining a thread does not let later chatter re-trigger paid turns.
        client.post(api + '/posts', json={'id':'thanks','room':'general','parent':'integration-root','body':'Thanks!'}).raise_for_status()
        time.sleep(0.4)
        assert len(receipts()) == expected_turns
        if not live:
            agent_id = next(a['id'] for a in status['agents'] if a['name'] == 'fable')
            client.post(api + '/tasks', json={'id':'website-task','title':'Website request',
                'request':'Review the existing setup without changing identities.', 'intent':'review',
                'criteria':['Existing connections keep working'], 'room':'general','owner':agent_id}).raise_for_status()
            for _ in range(500):
                work = client.get(api + '/work?task=website-task').json()
                if work['tasks'][0]['status'] == 'done' and any(r['state'] == 'done' for r in work['runs']):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Website task did not complete and report: ' + json.dumps(work))
            website_run = next(r for r in work['runs'] if r['state'] == 'done')
            assert website_run['runner'] == 'Kanbot' and not website_run['canCancel']
            assert work['tasks'][0]['result'] == 'Verified Review the existing setup without changing identities.'
            runs = [json.loads(line) for line in (directory/'runs.jsonl').read_text().splitlines()]
            assert runs[-1]['brief']['criteria'] == ['Existing connections keep working']
            assert runs[-1]['brief']['intent'] == 'review'
            expected_turns += 1
            # The website saves an unassigned first mission before any runner exists.
            client.post(api + '/tasks', json={'id':'saved-first-mission','title':'Saved onboarding goal',
                'request':'Read the saved onboarding context and verify the outcome.', 'intent':'review',
                'criteria':['The original task gets the result'], 'room':'general','owner':''}).raise_for_status()
            before_tasks = {t['id'] for t in client.get(api + '/tasks').json()['tasks']}
            submitted = cli('send','fable','--task','saved-first-mission','--request-id','onboarding-once')
            repeated = cli('send','fable','--task','saved-first-mission','--request-id','onboarding-once')
            assert submitted['job'] == repeated['job']
            for _ in range(500):
                mission_work = client.get(api + '/work?task=saved-first-mission').json()
                if mission_work['tasks'][0]['status'] == 'done' and any(r['state'] == 'done' for r in mission_work['runs']):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Attached mission did not finish: ' + json.dumps(mission_work))
            assert mission_work['tasks'][0]['owner'] == agent_id
            assert mission_work['tasks'][0]['result'].startswith('Verified Read the saved onboarding context')
            assert {t['id'] for t in client.get(api + '/tasks').json()['tasks']} == before_tasks
            assert cli('send','fable','--task','saved-first-mission','--request-id','new-reconnect-id')['job'] == submitted['job']
            conflicting = subprocess.run([sys.executable,'-m','kanbot','swarm','send','fable',
                '--task','website-task','--request-id','onboarding-once'], cwd=root, env=env, capture_output=True,text=True,timeout=35)
            assert conflicting.returncode and 'different work' in conflicting.stderr, conflicting.stderr
            runs = [json.loads(line) for line in (directory/'runs.jsonl').read_text().splitlines()]
            assert runs[-1]['brief']['id'] == 'saved-first-mission'
            assert runs[-1]['brief']['criteria'] == ['The original task gets the result']
            expected_turns += 1
        paused = cli('pause')
        assert paused['paused'] and not paused['running']
        # Reconnect preserves all completed jobs and doesn't repeat tools.
        assert cli('start')['running']
        time.sleep(0.4)
        assert len(receipts()) == expected_turns
        cli('pause')
        print(json.dumps({'passed':True,'live_models':live,'agents':expected_agents,'runtime_turns':expected_turns,
                          'runtimes':[root_runtime,peer_runtime] if live else ['claude','codex','kimi'],
                          'verified':['private registration','WebSocket inbox','peer delegation',
                                      'session continuation','thread return','no chatter loop','pause/reconnect']
                                     + ([] if live else ['nested delegation','parallel PTY execution','exact session resume','website task brief','run receipt','result in Work','saved mission attachment','no duplicate mission','submission task conflict'])}, indent=2))
    except Exception:
        print('Integration diagnostics retained at ' + str(directory), file=sys.stderr)
        # Keep the small test receipt on failure.
        temp._finalizer.detach()
        raise
    finally:
        if daemon:
            try:
                os.kill(daemon, signal.SIGTERM)
            except ProcessLookupError:
                pass
        server.terminate()
        server.wait(timeout=10)
        logs.close()
        client.close()
    temp.cleanup()


if __name__ == '__main__':
    main()
