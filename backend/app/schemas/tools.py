"""Pydantic v2 schemas for /tools 单独查询路由（M4.9）。

不走 Agent，给前端附加 UI 用（章节目录侧栏 / 术语 hover）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GlossarySearchBody(BaseModel):
    term: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=16, ge=1, le=64)


class GlossaryMatch(BaseModel):
    term: str
    normalized_term: str
    definition: str
    spec_id: str
    section_path: list[str]


class GlossarySearchResponse(BaseModel):
    items: list[GlossaryMatch]


class TocBody(BaseModel):
    spec_id: str = Field(min_length=1, max_length=32)
    section_prefix: list[str] = Field(default_factory=list)
    limit: int = Field(default=60, ge=1, le=200)


class TocItem(BaseModel):
    section_path: list[str]
    section_title: str
    chunk_id: str
    chunk_type: str


class TocResponse(BaseModel):
    spec_id: str
    section_prefix: list[str]
    items: list[TocItem]


__all__ = [
    "GlossaryMatch",
    "GlossarySearchBody",
    "GlossarySearchResponse",
    "TocBody",
    "TocItem",
    "TocResponse",
]
