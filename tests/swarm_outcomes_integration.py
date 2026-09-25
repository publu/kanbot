"""Explicit outcomes and lost status responses against a disposable local API."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse
import uuid
import httpx
from kanbot.swarm import Store, Swarm, SwarmAPI

async def main():
    origin = os.environ['SWARM_TEST_URL']
    if urlparse(origin).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Use a disposable local API')
    slug = 'outcomes-' + uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=origin) as owner:
        (await owner.post('/api/workspaces', json={'id':slug,'name':'Outcome fixture','visibility':'private'})).raise_for_status()
        api_url = origin + '/api/w/' + slug
        invite = (await owner.post(api_url + '/invites', json={'maxUses':1})).json()['token']
        with tempfile.TemporaryDirectory(dir='test-results') as directory:
            store = Store(directory)
            cfg = {'api':api_url,'workspace':origin+'/w/'+slug,'invite':invite,'allow':['operator'],
                   'runtimes':['codex'],'max_agents':1,'concurrency':1,'max_turns':10,'max_depth':1,
                   'mode':'read','directory':str(Path(directory).resolve()),'timeout':30}
            store.put('config','main',cfg)
            calls = []
            async def driver(job, agent, prompt):
                calls.append(job['id'])
                return {'text':json.dumps({'message':'Concrete evidence or missing input','status':job['prompt']})}
            class LostResponseAPI(SwarmAPI):
                lost = set()
                async def call(self, agent, path, body=None, invite=False):
                    result = await super().call(agent,path,body,invite)
                    if path == '/task-status' and body['id'] not in self.lost:
                        self.lost.add(body['id'])
                        raise httpx.ReadError('Lost response after task status committed')
                    return result
            api = LostResponseAPI(cfg)
            swarm = Swarm(None,store,api,driver)
            try:
                with patch('kanbot.swarm.shutil.which',return_value='/fixture/codex'):
                    agent = await swarm.register('outcome-agent','codex')
                for status in ('blocked','review','done'):
                    job = swarm.new_job('outcome-'+status,agent['id'],status,'thread-'+status)
                    swarm.save_job(job)
                    await swarm.process(job['id'])
                    assert store.get('job',job['id'])['status'] == 'delivering'
                    task = (await api.call(agent,'/tasks?id='+job['id']))['tasks'][0]
                    assert task['status'] == status, task
                    version = task['version']
                    await swarm.process(job['id'])
                    task = (await api.call(agent,'/tasks?id='+job['id']))['tasks'][0]
                    assert task['status'] == status and task['version'] == version, task
                    assert calls.count(job['id']) == 1
                    assert store.get('job',job['id'])['status'] == 'done'
                print(json.dumps({'passed':True,'outcomes':['blocked','review','done'],'lost_status_response':'No repeated execution or task write'}))
            finally:
                await swarm.stop()
                store.db.close()

if __name__ == '__main__':
    asyncio.run(main())
