"""共用一个回调的多站登录回归：外部平台全部 Mock，不访问正式账户。"""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.api import auth as auth_api
from app.auth.wakudemo import WakuDemoOAuthConfig, WakuDemoOAuthError, WakuDemoOAuthService

OLD_SITE = 'https://lianhuaguangdongmahjong.guoguo-labs.online/'
NEW_SITE = 'https://yaotongmahjong.guoguo-labs.online/'
API_BASE = 'https://www.bestguo.top:58000'
CALLBACK = f'{API_BASE}/api/login/callback'


def config():
    return WakuDemoOAuthConfig(
        client_id='test-client', base_url='https://waku.example',
        redirect_uri=CALLBACK, frontend_url=OLD_SITE, frontend_urls=(NEW_SITE,))


def test_multi_site_configuration_and_legacy_default(monkeypatch):
    monkeypatch.setenv('WAKUDEMO_CLIENT_ID', 'test-client')
    monkeypatch.setenv('WAKUDEMO_BASE_URL', 'https://waku.example')
    monkeypatch.setenv('WAKUDEMO_REDIRECT_URI', CALLBACK)
    monkeypatch.setenv('WAKUDEMO_FRONTEND_URL', OLD_SITE)
    monkeypatch.setenv('WAKUDEMO_FRONTEND_URLS', f' {NEW_SITE}, {OLD_SITE}, ')
    monkeypatch.setenv('WAKUDEMO_COOKIE_SECURE', 'true')
    monkeypatch.setenv('WAKUDEMO_COOKIE_SAMESITE', 'none')
    loaded = WakuDemoOAuthConfig.from_env()
    assert loaded.configured
    assert loaded.allowed_frontend_urls == (OLD_SITE, NEW_SITE)
    assert loaded.resolve_frontend_url() == OLD_SITE
    assert loaded.frontend_origins == frozenset({OLD_SITE[:-1], NEW_SITE[:-1]})
    assert loaded.browser_origins == loaded.frontend_origins | {API_BASE}
    monkeypatch.delenv('WAKUDEMO_FRONTEND_URL')
    assert WakuDemoOAuthConfig.from_env().default_frontend_url == NEW_SITE


@pytest.mark.parametrize('target', [
    'https://evil.example/', '//evil.example/',
    'https://yaotongmahjong.guoguo-labs.online.evil.example/',
    'https://yaotongmahjong.guoguo-labs.online@evil.example/',
    f'{NEW_SITE}redirect?url=https://evil.example/',
    f'{NEW_SITE}?next=https://evil.example/', '',
])
def test_unlisted_return_urls_are_rejected(target):
    with pytest.raises(WakuDemoOAuthError, match='return_url_not_allowed'):
        config().resolve_frontend_url(target)


def test_invalid_extra_site_disables_oauth_and_insecure_cookies_require_all_sites_local():
    assert not replace(config(), frontend_urls=('http://public.example/',)).configured
    local = replace(config(), redirect_uri='http://localhost:8000/api/login/callback',
                    frontend_url='http://localhost:5173/', cookie_secure=False,
                    cookie_samesite='lax')
    assert not local.configured  # 额外站点仍是公网 HTTPS，不能使用不安全 Cookie。
    assert replace(local, frontend_urls=('http://localhost:4173/',)).configured


@pytest.mark.parametrize('target', [None, OLD_SITE, NEW_SITE])
@pytest.mark.parametrize('outcome', ['success', 'cancel', 'timeout', 'pkce'])
async def test_callback_returns_to_initiating_site_and_keeps_registered_callback(
        monkeypatch, target, outcome):
    async def provider(request):
        if request.url.path.endswith('/token'):
            assert parse_qs(request.content.decode())['redirect_uri'] == [CALLBACK]
            if outcome == 'timeout':
                raise httpx.ReadTimeout('timeout', request=request)
            if outcome == 'pkce':
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': 'server-only', 'expires_in': 600})
        return httpx.Response(200, json={'uid': 'same-user', 'displayName': '玩家'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        service = WakuDemoOAuthService(config(), upstream)
        monkeypatch.setattr(auth_api, 'get_wakudemo_oauth_service', lambda: service)
        app = FastAPI()
        app.include_router(auth_api.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=API_BASE, follow_redirects=False) as client:
            start = await client.get('/api/login/wakudemo',
                                     params={'return_to': target} if target else {})
            query = parse_qs(urlsplit(start.headers['location']).query)
            assert query['redirect_uri'] == [CALLBACK]
            assert 'return_to' not in query
            assert query['code_challenge_method'] == ['S256']
            assert 'domain=' not in start.headers['set-cookie'].lower()
            params = {'state': query['state'][0], 'return_to': 'https://evil.example/'}
            params.update({'error': 'access_denied'} if outcome == 'cancel' else {'code': 'code'})
            callback = await client.get('/api/login/callback', params=params)
            destination = urlsplit(callback.headers['location'])
            assert f'{destination.scheme}://{destination.netloc}{destination.path}' == (target or OLD_SITE)
            result = parse_qs(destination.query)
            if outcome == 'success':
                assert result == {'wakudemo_login': ['success']}
                for origin in (OLD_SITE[:-1], NEW_SITE[:-1]):
                    session = await client.get('/api/login/session', headers={'Origin': origin})
                    assert session.json()['account']['id'] == 'same-user'
                    assert 'server-only' not in session.text
            else:
                expected = {'cancel': 'authorization_cancelled', 'timeout': 'token_endpoint_timeout',
                            'pkce': 'pkce_or_authorization_code_rejected'}[outcome]
                assert result['wakudemo_error'] == [expected]


async def test_rejected_return_to_and_old_tab_do_not_destroy_new_site_transaction(monkeypatch):
    service = WakuDemoOAuthService(config())
    monkeypatch.setattr(auth_api, 'get_wakudemo_oauth_service', lambda: service)
    app = FastAPI()
    app.include_router(auth_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=API_BASE, follow_redirects=False) as client:
        old_start = await client.get('/api/login/wakudemo')
        old_state = parse_qs(urlsplit(old_start.headers['location']).query)['state'][0]
        new_start = await client.get('/api/login/wakudemo', params={'return_to': NEW_SITE})
        new_state = parse_qs(urlsplit(new_start.headers['location']).query)['state'][0]
        transaction = client.cookies.get(config().transaction_cookie_name)
        for params in ({'return_to': 'https://evil.example/'},
                       [('return_to', NEW_SITE), ('return_to', OLD_SITE)]):
            rejected = await client.get('/api/login/wakudemo', params=params)
            assert rejected.status_code == 400
            assert 'location' not in rejected.headers
            assert 'set-cookie' not in rejected.headers
        stale = await client.get('/api/login/callback', params={'code': 'old', 'state': old_state})
        assert stale.headers['location'].startswith(NEW_SITE)
        assert 'state_mismatch' in stale.headers['location']
        assert client.cookies.get(config().transaction_cookie_name) == transaction
        cancelled = await client.get('/api/login/callback',
                                     params={'error': 'access_denied', 'state': new_state})
        assert cancelled.headers['location'].startswith(NEW_SITE)
        assert 'authorization_cancelled' in cancelled.headers['location']


async def test_expired_transaction_and_missing_cookie_return_safely(monkeypatch):
    service = WakuDemoOAuthService(config())
    monkeypatch.setattr(auth_api, 'get_wakudemo_oauth_service', lambda: service)
    app = FastAPI()
    app.include_router(auth_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=API_BASE, follow_redirects=False) as client:
        start = await client.get('/api/login/wakudemo', params={'return_to': NEW_SITE})
        state = parse_qs(urlsplit(start.headers['location']).query)['state'][0]
        transaction = client.cookies.get(config().transaction_cookie_name)
        service._pending[transaction].expires_at = 0
        expired = await client.get('/api/login/callback', params={'state': state, 'code': 'expired'})
        assert expired.headers['location'].startswith(NEW_SITE)
        assert 'authorization_request_expired' in expired.headers['location']
        # 服务端事务彻底丢失后，不从回调参数猜测返回站点。
        client.cookies.clear()
        missing = await client.get('/api/login/callback',
                                   params={'state': state, 'code': 'lost', 'return_to': NEW_SITE})
        assert missing.headers['location'].startswith(OLD_SITE)
        assert 'authorization_request_expired' in missing.headers['location']


async def test_login_start_errors_also_return_to_selected_site(monkeypatch):
    service = WakuDemoOAuthService(replace(config(), login_rate_limit_per_minute=1))
    monkeypatch.setattr(auth_api, 'get_wakudemo_oauth_service', lambda: service)
    app = FastAPI()
    app.include_router(auth_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=API_BASE, follow_redirects=False) as client:
        await client.get('/api/login/wakudemo')
        limited = await client.get('/api/login/wakudemo', params={'return_to': NEW_SITE})
        assert limited.headers['location'].startswith(NEW_SITE)
        assert 'oauth_rate_limited' in limited.headers['location']


@pytest.mark.parametrize('origin', [OLD_SITE[:-1], NEW_SITE[:-1], 'https://evil.example'])
async def test_session_and_logout_origins_include_both_sites_only(monkeypatch, origin):
    service = WakuDemoOAuthService(config())
    session_id = service.create_session('server-only', 600, {'id': 'same-user'})
    monkeypatch.setattr(auth_api, 'get_wakudemo_oauth_service', lambda: service)
    app = FastAPI()
    app.include_router(auth_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API_BASE) as client:
        client.cookies.set(config().cookie_name, session_id)
        session = await client.get('/api/login/session', headers={'Origin': origin})
        logout = await client.post('/api/login/logout',
                                   headers={'Origin': origin, 'Content-Type': 'application/json'})
        allowed = origin != 'https://evil.example'
        assert session.status_code == logout.status_code == (200 if allowed else 403)
        assert (service.get_session(session_id) is None) == allowed
