"""Admin API dependencies: JWT bearer auth resolving to an admin ``User``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from src.core.enums import Role, UserStatus
from src.core.security import jwt_decode
from src.infrastructure.di import AppContainer
from src.web.deps import get_container


@dataclass(slots=True)
class AdminIdentity:
    user_id: int
    username: str
    role: Role


def _unauthorized(detail: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def require_admin(
    request: Request, container: AppContainer = Depends(get_container)
) -> AdminIdentity:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized()
    payload = jwt_decode(auth.removeprefix("Bearer "), container.settings.app.jwt_secret)
    if payload is None or payload.get("scope") != "admin":
        raise _unauthorized()

    async with container.uow() as uow:
        user = await uow.users.get(int(payload["sub"]))
    if user is None or user.status is not UserStatus.ACTIVE:
        raise _unauthorized("admin access revoked")

    # Read-only demo sessions: PREVIEW role may look at everything but change nothing.
    if user.role is Role.PREVIEW:
        if not container.settings.admin.demo_enabled:
            raise _unauthorized("demo mode disabled")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="demo is read-only",
            )
    elif not user.role.is_staff:
        raise _unauthorized("admin access revoked")
    return AdminIdentity(user_id=user.id, username=user.username or f"id{user.id}", role=user.role)


async def require_owner(identity: AdminIdentity = Depends(require_admin)) -> AdminIdentity:
    """Panel credentials are more sensitive than ordinary bot settings."""
    if identity.role is not Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner access required")
    return identity
