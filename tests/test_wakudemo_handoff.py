"""Safari 场景：完成顶层 OAuth 后，所有跨站 API 请求均无 Cookie。"""
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api import auth, deps
from app.auth.wakudemo import (
    WakuDemoOAuthConfig, WakuDemoOAuthError, WakuDemoOAuthService, create_s256_challenge,
)

ORIGIN = 'https://yaotong.example'
API = 'https://api.example'
VERIFIER = 'v' * 43


def config():
    return WakuDemoOAuthConfig(client_id='test', base_url='https://waku.example',
                              redirect_uri=f'{API}/api/login/callback', frontend_url=f'{ORIGIN}/')


async def test_cookie_blocked_login_protected_api_reload_and_logout(monkeypatch):
    async def provider(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'upstream-secret', 'expires_in': 600})
        return httpx.Response(200, json={'uid': 'player-1', 'displayName': '测试玩家'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        service = WakuDemoOAuthService(config(), upstream)
        monkeypatch.setattr(auth, 'get_wakudemo_oauth_service', lambda: service)
        monkeypatch.setattr(deps, 'get_wakudemo_oauth_service', lambda: service)
        app = FastAPI()
        app.include_router(auth.router)

        @app.get('/protected')
        async def protected(user=Depends(deps.require_wakudemo_login)):
            return {'id': user.player_id}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=API) as client:
            start = await client.get('/api/login/wakudemo', params={
                'return_to': f'{ORIGIN}/', 'session_challenge': create_s256_challenge(VERIFIER),
            })
            state = parse_qs(urlsplit(start.headers['location']).query)['state'][0]
            callback = await client.get('/api/login/callback', params={'state': state, 'code': 'good-code'})
            destination = urlsplit(callback.headers['location'])
            assert parse_qs(destination.query) == {'wakudemo_login': ['success']}
            ticket = parse_qs(destination.fragment)['wakudemo_ticket'][0]
            assert 'upstream-secret' not in callback.headers['location']
            # 模拟 Safari：API fetch 不携带顶层回调取得的第三方 Cookie。
            client.cookies.clear()
            headers = {'Origin': ORIGIN}
            assert (await client.get('/api/login/session', headers=headers)).json() == {'authenticated': False}
            for origin, verifier in [('https://evil.example', VERIFIER), (ORIGIN, 'x' * 43)]:
                rejected = await client.post('/api/login/exchange', headers={'Origin': origin},
                                             json={'ticket': ticket, 'verifier': verifier})
                assert rejected.status_code == 401
            exchange = await client.post('/api/login/exchange', headers=headers,
                                         json={'ticket': ticket, 'verifier': VERIFIER})
            assert exchange.status_code == 200
            assert exchange.headers['cache-control'] == 'no-store'
            assert 'upstream-secret' not in exchange.text
            headers['Authorization'] = f"Bearer {exchange.json()['sessionToken']}"
            for _ in range(2):
                session = await client.get('/api/login/session', headers=headers)
                assert session.json()['account']['id'] == 'player-1'
                assert (await client.get('/protected', headers=headers)).json() == {'id': 'wakudemo-player-1'}
            replay = await client.post('/api/login/exchange', headers=headers,
                                       json={'ticket': ticket, 'verifier': VERIFIER})
            assert replay.status_code == 401
            assert (await client.post('/api/login/logout', headers=headers, json={})).status_code == 200
            assert (await client.get('/protected', headers=headers)).status_code == 401


def test_handoff_expires_and_cannot_resurrect_revoked_sessions(monkeypatch):
    service = WakuDemoOAuthService(config())
    _, tx = service.begin(session_challenge=create_s256_challenge(VERIFIER))
    pending = service._pending[tx]
    sid = service.create_session('upstream', 600, {'id': 'a'})
    ticket = service.create_handoff(sid, pending)
    service._handoffs[ticket].expires_at = 0
    with pytest.raises(WakuDemoOAuthError, match='session_handoff_rejected'):
        service.redeem_handoff(ticket, VERIFIER, ORIGIN)
    ticket = service.create_handoff(sid, pending)
    service.delete_session(sid)
    with pytest.raises(WakuDemoOAuthError, match='session_handoff_rejected'):
        service.redeem_handoff(ticket, VERIFIER, ORIGIN)


def test_malformed_authorization_does_not_fall_back_to_cookie():
    from starlette.requests import Request
    from app.auth.wakudemo import request_session_id
    request = Request({'type': 'http', 'headers': [
        (b'authorization', b'Bearer invalid'), (b'cookie', b'lgm_wakudemo_session=valid-cookie'),
    ]})
    assert request_session_id(request, WakuDemoOAuthService(config())) is None
