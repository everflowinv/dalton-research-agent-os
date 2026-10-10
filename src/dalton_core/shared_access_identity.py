"""An explicit authenticated shared visitor, never a Tailscale owner identity."""
from __future__ import annotations
import hashlib

class SharedLogin(str):
    """Only the trusted gateway authentication branch constructs this value."""
    def __new__(cls, principal: str):
        if not isinstance(principal, str) or not principal or len(principal)>100:
            raise ValueError('invalid shared principal')
        return str.__new__(cls, 'shared-access:' + principal)

def shared_actor(login: object) -> str | None:
    if isinstance(login, SharedLogin):
        return 'human:shared-' + hashlib.sha256(str(login).encode()).hexdigest()[:32]
    return None

def is_shared_login(login: object) -> bool:
    return isinstance(login, SharedLogin)
