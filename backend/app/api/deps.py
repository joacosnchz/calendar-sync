from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.security import SESSION_COOKIE, InvalidToken, read_session
from app.services import Services

CSRF_HEADER = "x-calsync-csrf"


def get_services(request: Request) -> Services:
    return request.app.state.services


async def get_session(services: Services = Depends(get_services)) -> AsyncIterator[AsyncSession]:
    async with services.sessionmaker() as session:
        yield session


async def current_user(
    request: Request,
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        user_id = read_session(services.settings, token)
    except InvalidToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


async def require_operator(user: User = Depends(current_user)) -> User:
    if not user.is_operator:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator access required")
    return user


async def csrf_protect(request: Request) -> None:
    """Session cookies are SameSite=Lax; mutating calls must also carry a custom header,
    which a cross-site form or simple request cannot set."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF header")
