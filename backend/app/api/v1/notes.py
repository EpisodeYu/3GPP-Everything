"""`/api/v1/notes` 笔记 CRUD（M4.9）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.errors import NotFoundError
from app.db.base import get_db
from app.db.models import Note, User
from app.schemas.notes import (
    NoteCreateBody,
    NoteListResponse,
    NoteOut,
    NotePatchBody,
    TargetType,
)

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=NoteOut)
async def create_note(
    body: NoteCreateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NoteOut:
    n = Note(
        user_id=user.id,
        target_type=body.target_type,
        target_id=body.target_id,
        body=body.body,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return NoteOut.model_validate(n, from_attributes=True)


@router.get("", response_model=NoteListResponse)
async def list_notes(
    target_type: TargetType | None = Query(default=None),
    target_id: str | None = Query(default=None, max_length=128),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NoteListResponse:
    stmt = select(Note).where(Note.user_id == user.id)
    if target_type:
        stmt = stmt.where(Note.target_type == target_type)
    if target_id:
        stmt = stmt.where(Note.target_id == target_id)
    stmt = stmt.order_by(Note.updated_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    items = [NoteOut.model_validate(r, from_attributes=True) for r in rows]
    return NoteListResponse(items=items)


@router.patch("/{nid}", response_model=NoteOut)
async def patch_note(
    nid: uuid.UUID,
    body: NotePatchBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NoteOut:
    n = await _load_owned(db, nid, user.id)
    n.body = body.body
    await db.commit()
    await db.refresh(n)
    return NoteOut.model_validate(n, from_attributes=True)


@router.delete("/{nid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    nid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    n = await _load_owned(db, nid, user.id)
    await db.delete(n)
    await db.commit()


async def _load_owned(db: AsyncSession, nid: uuid.UUID, user_id: uuid.UUID) -> Note:
    stmt = select(Note).where(Note.id == nid, Note.user_id == user_id)
    n = (await db.execute(stmt)).scalar_one_or_none()
    if n is None:
        raise NotFoundError("note_not_found", code="note_not_found")
    return n
