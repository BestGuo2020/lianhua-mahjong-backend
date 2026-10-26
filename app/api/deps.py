"""联机接口统一登录鉴权依赖。

WakuDemo 登录后，联机身份一律由服务端从登录会话推导（uid），不再信任客户端
提交的 playerId / guestId。身份键统一为 ``wakudemo-<uid>``，避免与历史匿名
guestId 冲突；封禁、防占房、战绩与头像都绑定该键。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request

from app.auth.wakudemo import get_wakudemo_oauth_service, request_session_id

AUTH_REQUIRED = 'AUTH_REQUIRED'


@dataclass(frozen=True)
class AuthenticatedUser:
    """从 WakuDemo 会话推导出的联机玩家身份。"""

    uid: str
    display_name: Optional[str]
    avatar_url: Optional[str]

    @property
    def player_id(self) -> str:
        return f'wakudemo-{self.uid}'


async def require_wakudemo_login(request: Request) -> AuthenticatedUser:
    """联机接口鉴权：无有效登录会话 → 401 AUTH_REQUIRED。

    本地开发（WAKUDEMO_LOGIN_BYPASS=true）且无真实会话时，按客户端提交的
    playerId 推导测试身份（非法/缺失时用 dev-bypass），跳过 OAuth 流程。
    """
    service = get_wakudemo_oauth_service()
    session = service.get_session(request_session_id(request, service))
    if session is not None:
        account: dict[str, Any] = session.account or {}
        uid = account.get('id')
        if not isinstance(uid, str) or not uid:
            raise HTTPException(status_code=401, detail={'code': AUTH_REQUIRED})
        display_name = account.get('displayName')
        avatar_url = account.get('avatarUrl')
        return AuthenticatedUser(
            uid=uid,
            display_name=display_name if isinstance(display_name, str) else None,
            avatar_url=avatar_url if isinstance(avatar_url, str) else None,
        )
    if service.config.login_bypass:
        return AuthenticatedUser(
            uid=await _bypass_uid(request),
            display_name='本地开发账号',
            avatar_url=None,
        )
    raise HTTPException(status_code=401, detail={'code': AUTH_REQUIRED})


async def _bypass_uid(request: Request) -> str:
    """开发旁路身份：优先客户端 playerId（保持多开/多身份测试体验），否则固定值。"""
    try:
        payload = await request.json()
    except Exception:
        return 'dev-bypass'
    if isinstance(payload, dict):
        candidate = payload.get('playerId')
        if isinstance(candidate, str) and candidate.strip() \
                and len(candidate) <= 64 \
                and all(ord(ch) < 128 for ch in candidate):
            return candidate.strip()
    return 'dev-bypass'
