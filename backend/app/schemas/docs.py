"""Pydantic v2 schemas for /docs Reader（M4.9）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocOut(BaseModel):
    spec_id: str
    release: str
    series: str
    title: str = ""
    chunk_count: int = 0


class DocListResponse(BaseModel):
    items: list[DocOut]
    total: int


class SectionNode(BaseModel):
    section_path: list[str]
    section_title: str
    chunk_count: int


class DocDetailResponse(BaseModel):
    spec_id: str
    release: str = ""
    series: str = ""
    sections: list[SectionNode]


class ChunkOut(BaseModel):
    chunk_id: str
    spec_id: str
    section_path: list[str]
    section_title: str
    chunk_type: str
    content: str
    char_offset_start: int | None = None
    char_offset_end: int | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)


class SectionDetailResponse(BaseModel):
    spec_id: str
    section_path: list[str]
    section_title: str
    chunks: list[ChunkOut]


class SearchHit(BaseModel):
    chunk_id: str
    spec_id: str
    section_path: list[str]
    section_title: str
    chunk_type: str
    preview: str


class SearchResponse(BaseModel):
    spec_id: str
    query: str
    items: list[SearchHit]
