"""Quick test: see what /run events look like when fetch_questions is called."""
import json
import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=30) as c:
        # Create session
        r = await c.post('http://127.0.0.1:8000/apps/Home_Agent/users/user/sessions',
                         json={'state': {'user:std': 8, 'user:level': 'home'}})
        sid = r.json()['id']
        print(f'Session: {sid}\n')

        # Send message that triggers fetch_questions
        r = await c.post('http://127.0.0.1:8000/run', json={
            'app_name': 'Home_Agent',
            'user_id': 'user',
            'session_id': sid,
            'new_message': {'parts': [{'text': 'where is the key'}]},
            'streaming': False,
        })
        events = r.json()
        print(f'Got {len(events)} events\n')
        for i, ev in enumerate(events):
            content = ev.get('content', {})
            parts = content.get('parts', [])
            author = ev.get('author', '?')
            for j, p in enumerate(parts):
                keys = list(p.keys())
                print(f'Event[{i}] author={author} part[{j}] keys={keys}')
                if 'text' in p:
                    print(f'  text: {p["text"][:200]}')
                else:
                    print(f'  FULL: {json.dumps(p, default=str)[:500]}')
                print()

asyncio.run(test())
