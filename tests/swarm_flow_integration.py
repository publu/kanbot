"""Private real API flow checks; deterministic models, no external messages."""
import asyncio
import json
from unittest.mock import patch

import httpx

import swarm_durability_integration as harness
from kanbot.swarm import Store, Swarm, SwarmAPI, stable


async def scenario(origin, directory):
    async with httpx.AsyncClient(base_url=origin) as owner:
        (await owner.post('/api/workspaces', json={'id': 'flows', 'name': 'Flow checks', 'visibility': 'private'})).raise_for_status()
        api_url = origin + '/api/w/flows'
        invite = (await owner.post(api_url + '/invites', json={'maxUses': 10})).json()['token']
        cfg = {'api': api_url, 'workspace': origin + '/w/flows', 'invite': invite,
               'allow': ['operator'], 'runtimes': ['claude', 'hermes'], 'max_agents': 4,
               'concurrency': 2, 'max_turns': 5, 'max_depth': 4, 'mode': 'read',
               'directory': str(directory), 'timeout': 30}
        stores = [Store(directory / name) for name in ('parent', 'remote', 'replacement')]
        for store in stores:
            store.put('config', 'main', cfg)
        entered, stopped, renew = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def driver(job, agent, prompt):
            calls.append(job['id'])
            if agent['runtime'] == 'claude':
                return {'text': json.dumps({'message': 'Delegating', 'delegate': [{'to': 'reviewer', 'request': 'Review'}]})}
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        a, b, c = [Swarm(None, store, SwarmAPI(cfg), driver) for store in stores]
        try:
            with patch('kanbot.swarm.shutil.which', return_value='/fixture/runtime'):
                lead = await a.register('lead', 'claude')
                peer = await b.register('reviewer', 'hermes')
            await a.refresh_directory()
            job = a.new_job('cancel-root', lead['id'], 'Delegate a review', 'root-thread')
            a.save_job(job)
            await a.process(job['id'])
            root = stores[0].get('job', job['id'])
            assert root['status'] == 'waiting', root
            child = stores[0].get('job', root['children'][0])
            await a.process(child['id'])
            stores[1].put('config', 'main', {**cfg, 'allow': [lead['id']]})
            await b.ingest(peer, {'id': 100, 'type': 'message', 'actor': lead['id'], 'objectId': child['thread']})
            original_sleep = asyncio.sleep

            async def controlled_sleep(seconds):
                if seconds == 25:
                    await renew.wait()
                else:
                    await original_sleep(seconds)

            with patch('kanbot.swarm.asyncio.sleep', controlled_sleep):
                task = asyncio.create_task(b.process(child['id']))
                b.active[child['id']] = task
                await asyncio.wait_for(entered.wait(), 5)
                child_cancelled = await a.cancel(child['id'])
                assert not child_cancelled['pending']
                assert stores[0].get('job', job['id'])['status'] == 'waiting'
                graph = (await a.api.call(lead, '/executions?root=cancel-root'))['executions']
                assert next(e for e in graph if e['job'] == child['id'])['cancelled']
                assert not next(e for e in graph if e['job'] == job['id']).get('cancelled')
                # Simulate a dropped cancellation request. Local cancellation
                # persists an outbox record; the next healthy pass sends it.
                original_call = a.api.call

                async def unavailable(agent, path, body=None, **kwargs):
                    if path == '/executions' and body and body.get('action') == 'cancel':
                        raise httpx.ConnectError('Injected cancellation outage')
                    return await original_call(agent, path, body, **kwargs)

                with patch.object(a.api, 'call', unavailable):
                    cancelled = await a.cancel(job['id'])
                assert cancelled['pending']
                assert stores[0].get('job', job['id'])['status'] == 'cancelled'
                await a.flush_cancellations()
                assert all(p['sent'] for p in stores[0].all('cancellation'))
                assert not a.status()['error'], 'A completed cancellation retained a stale outage warning'
                renew.set()
                await asyncio.wait_for(task, 5)
                assert stopped.is_set(), 'Remote runtime did not stop after lease revocation'
                assert stores[1].get('job', child['id'])['status'] == 'cancelled'
            records = (await a.api.call(lead, '/executions?root=cancel-root'))['executions']
            assert len(records) == 2 and all(e['cancelled'] for e in records)
            for agent in (lead, peer):
                stores[2].put('agent', agent['id'], agent)
            await c.recover_shared()
            assert len(stores[2].all('job')) == 2
            assert all(j['status'] == 'cancelled' for j in stores[2].all('job'))
            for recovered in stores[2].all('job'):
                await c.process(recovered['id'])
            assert len(calls) == 2, 'Cancelled work executed again'
            paused_job = a.new_job('paused-cancel', lead['id'], 'Delegate another review', 'paused-thread')
            a.save_job(paused_job)
            await a.process(paused_job['id'])
            assert stores[0].get('job', paused_job['id'])['status'] == 'waiting'
            await a.stop()
            a = Swarm(None, stores[0], driver=driver)  # A new, still-paused runner instance.
            cancelled_paused = await a.cancel(paused_job['id'])
            assert not cancelled_paused['pending']
            assert a.status()['paused'] and not a.running and a.api is None
            saved_paused = (await c.api.call(lead, '/executions?job=paused-cancel'))['executions']
            assert saved_paused and saved_paused[0]['cancelled']
            print(json.dumps({'passed': True, 'verified': ['cancel waiting parent', 'remote runtime stopped',
                'cancel remote child independently',
                'cancel while paused without resuming',
                'outage retains cancellation', 'retry persists cancellation', 'fresh host preserves cancellation',
                'no duplicate model calls']}, indent=2))
        finally:
            for swarm in (a, b, c):
                for task in swarm.active.values():
                    task.cancel()
                await asyncio.gather(*swarm.active.values(), return_exceptions=True)
                await swarm.stop()
            for store in stores:
                store.db.close()


if __name__ == '__main__':
    harness.scenario = scenario
    harness.main()
