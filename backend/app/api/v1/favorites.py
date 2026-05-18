"""`/api/v1/favorites` 收藏 CRUD（M4.9）。

文档锚 04-backend-api.md §2 Favorites。简化为 POST/GET/DELETE：
- POST /favorites               → 新增（target_type ∈ {chunk, message}）
- GET  /favorites?target_type=  → 列出当前用户的收藏
- DELETE /favorites/{fid}       → 删除自己的收藏

权限：登录 user；只能看 / 改自己的收藏。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.errors import NotFoundError
from app.db.base import get_db
from app.db.models import Favorite, User
from app.schemas.favorites import (
    FavoriteCreateBody,
    FavoriteListResponse,
    FavoriteOut,
    TargetType,
)

router = APIRouter(prefix="/favorites", tags=["favorites"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FavoriteOut)
async def create_favorite(
    body: FavoriteCreateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteOut:
    f = Favorite(user_id=user.id, target_type=body.target_type, target_id=body.target_id)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return FavoriteOut.model_validate(f, from_attributes=True)


@router.get("", response_model=FavoriteListResponse)
async def list_favorites(
    target_type: TargetType | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteListResponse:
    stmt = select(Favorite).where(Favorite.user_id == user.id)
    if target_type:
        stmt = stmt.where(Favorite.target_type == target_type)
    stmt = stmt.order_by(Favorite.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    items = [FavoriteOut.model_validate(r, from_attributes=True) for r in rows]
    return FavoriteListResponse(items=items)


@router.delete("/{fid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    fid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    stmt = select(Favorite).where(Favorite.id == fid, Favorite.user_id == user.id)
    f = (await db.execute(stmt)).scalar_one_or_none()
    if f is None:
        raise NotFoundError("favorite_not_found", code="favorite_not_found")
    await db.delete(f)
    await db.commit()
