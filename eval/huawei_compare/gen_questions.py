"""华为对比测试 · 100 题中立题集生成器（采样 → LLM 生成 → R18 核验 → 选 100）。

流程（设计见 gen_prompts.py 顶部 + README §2-3）：
1. **采样脚手架** = A 的 by_spec/*.jsonl chunk（带 clause + chunk_type）。按 category↔
   chunk_type 反查（table_lookup←table / formula←formula / 其余←text），在 R18 交集
   spec 内按 series 配额采样，过采样。
2. **生成** = mimo-v2.5-pro 异步批量（复用 teleqna.infer 的 _LiteLLMChatClient /
   _RpmLimiter / _extract_json），每段 chunk 出一题；negative 走两种专门 prompt。
3. **R18 公平核验**（仅 positive）= expected_facts 去 B 的 R18 全文（Documents.db）核验，
   覆盖率低于阈值 → 疑似 R19-only → skip（保证存活题 R18 两库都检得到）。
4. **选 100** = 按 category 目标配额 + series 轮转选齐 → golden_compare.yaml（golden
   schema，直接喂 validators/golden.py + fact_coverage_judge + negative_judge）。

用法（eval venv）：
    PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python -m eval.huawei_compare.gen_questions \
        --out eval/huawei_compare/golden_compare.yaml [--n 100] [--oversample 1.4] [--seed 42]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval.huawei_compare.build_intersection import default_a_dir, normalize_spec_id
from eval.huawei_compare.gen_prompts import (
    build_false_premise_messages,
    build_multi_section_messages,
    build_out_of_lib_messages,
    build_positive_messages,
)
from eval.settings import EvalSettings, get_settings
from eval.teleqna.infer import (
    DEFAULT_TIMEOUT_S,
    _extract_json,
    _LiteLLMChatClient,
    _RpmLimiter,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
INTERSECTION_PATH = HERE / "r18_intersection_specs.txt"
DEFAULT_OUT = HERE / "golden_compare.yaml"
B_DB_PATH = Path("/data/telco-rag/Telco-RAG_api/3GPP-Release18/Documents.db")

# --- 配额 -----------------------------------------------------------------
# positive 84 题的 series 配额（README §4 ×0.84 取整，和=84）
POSITIVE_SERIES_QUOTA: dict[str, int] = {
    "23": 17,
    "38": 14,
    "29": 13,
    "24": 10,
    "33": 9,
    "36": 5,
    "32": 3,
    "26": 3,
    "37": 2,
    "22": 2,
    "31": 1,
    "28": 5,
}
# positive 84 题的 category 配额（和=84）
POSITIVE_CATEGORY_TARGETS: dict[str, int] = {
    "definition": 22,
    "procedure": 20,
    "table_lookup": 16,
    "formula": 12,
    "multi_section": 14,
}
# category → 采样所需 chunk_type
CATEGORY_CHUNK_TYPE: dict[str, str] = {
    "definition": "text",
    "procedure": "text",
    "multi_section": "text",
    "table_lookup": "table",
    "formula": "formula",
}
# negative 16 题：8 false-premise + 8 out-of-lib
NEG_FALSE_PREMISE_TARGET = 8
NEG_OUT_OF_LIB_TARGET = 8

NEG_FALSE_PREMISE_DOMAINS = [
    "5G NAS registration & mobility management (TS 24.501)",
    "5G System architecture & PDU sessions (TS 23.501)",
    "NR RRC connection control (TS 38.331)",
    "5G security & authentication (TS 33.501)",
    "Service-based interfaces / SBI (TS 29.5xx)",
    "NR MAC layer & scheduling (TS 38.321)",
    "Policy & charging control (TS 23.503 / 32.xxx)",
    "NR physical layer procedures (TS 38.213 / 38.214)",
]
NEG_OUT_OF_LIB_AREAS = [
    "Ambient IoT (Rel-19 normative work)",
    "AI/ML for the NR air interface (Rel-19 normative)",
    "Integrated Sensing and Communication / ISAC (Rel-19)",
    "Non-Terrestrial Network enhancements (Rel-19)",
    "Network energy savings (Rel-19)",
    "5G-Advanced multicast/broadcast enhancements (Rel-19)",
    "XR (Extended Reality) media enhancements (Rel-19)",
    "Personal IoT / Ambient power networks (Rel-19)",
]

# 采样 chunk 的质量过滤
_BOILERPLATE_TITLES = (
    "copyright",
    "foreword",
    "references",
    "scope",
    "void",
    "change history",
    "modal verbs",
    "table of contents",
    "annex a (informative)",
)
MIN_TEXT_LEN = 220
MIN_TABLE_LEN = 120
MIN_FORMULA_LEN = 40
MAX_EXCERPT_CHARS = 3500  # 喂 LLM 的 chunk 上限

# 生成/校验参数
DEFAULT_MAX_TOKENS = 4096
DEFAULT_RPM = 50
DEFAULT_CONCURRENT = 8
R18_COVERAGE_MIN = 0.5  # positive 题 expected_facts 在 B 的 R18 全文里的最低覆盖率

# golden schema 约束（同 builder/transform.py）
MIN_FACTS = 3
MAX_FACTS = 7
MAX_FORBIDDEN = 4
MAX_QUESTION_CHARS = 1000

_HEADER_RE = re.compile(r"^\[[^\]]+\]\s*", re.MULTILINE)  # 去掉 "[23.501 § 1 Scope]" 前缀
_NONWORD_RE = re.compile(r"[^a-z0-9]+")

# 采样源排除：测试/一致性/RF/EMC/study 规范——非 RAG 用户常问的"知识"题源。
# 交集里所有多部件 -N spec 都是测试/study（36.521-x/37.145-x/38.101-x/38.141-x/38.521-x/
# 23.700-xx）→ 全排；再补几个单部件一致性/EMC spec。仅排"采样源"，expected_specs 白名单
# 仍用全交集（核心题偶尔引一篇测试 spec 没问题）。
_MULTIPART_RE = re.compile(r"-\d+$")
EXCLUDE_SAMPLING_SPECS: frozenset[str] = frozenset(
    {"37.141", "38.113", "38.114", "38.124", "38.508", "38.509", "38.522", "38.533"}
)


def is_excluded_spec(spec_id: str) -> bool:
    return bool(_MULTIPART_RE.search(spec_id)) or spec_id in EXCLUDE_SAMPLING_SPECS


# === 数据加载 =============================================================


def load_intersection_specs(path: Path = INTERSECTION_PATH) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def specs_by_series(specs: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in specs:
        out.setdefault(s[:2], []).append(s)
    return out


def chunk_text(chunk: dict) -> str:
    """去掉 "[spec § clause title]" 头，返回正文。"""
    return _HEADER_RE.sub("", str(chunk.get("content") or ""), count=1).strip()


def _is_usable(chunk: dict, chunk_type: str) -> bool:
    title = str(chunk.get("section_title") or "").lower()
    if any(b in title for b in _BOILERPLATE_TITLES):
        return False
    body = chunk_text(chunk)
    min_len = {"text": MIN_TEXT_LEN, "table": MIN_TABLE_LEN, "formula": MIN_FORMULA_LEN}.get(
        chunk_type, MIN_TEXT_LEN
    )
    return len(body) >= min_len


def load_spec_chunks(spec_id: str, by_spec_dir: Path) -> list[dict]:
    p = by_spec_dir / f"{spec_id}.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# === 采样 =================================================================


def plan_slots(
    category_targets: dict[str, int],
    series_quota: dict[str, int],
    oversample: float,
) -> list[tuple[str, str]]:
    """生成 (category, series) 采样槽位表（category-major；series 用最大余数法分配）。

    每个 category 的总数 = ceil(target * oversample)，再按 series_quota 占比分到各 series。
    """
    slots: list[tuple[str, str]] = []
    series_total = sum(series_quota.values()) or 1
    for cat, target in category_targets.items():
        n = max(1, round(target * oversample))
        # 最大余数法：先按比例取整，余量给余数最大的 series
        raw = {s: n * q / series_total for s, q in series_quota.items()}
        base = {s: int(v) for s, v in raw.items()}
        remainder = n - sum(base.values())
        for s, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[
            :remainder
        ]:
            base[s] += 1
        for s, cnt in base.items():
            slots.extend([(cat, s)] * cnt)
    return slots


@dataclass(slots=True)
class GenJob:
    """一个待生成槽位。"""

    kind: str  # positive | multi_section | false_premise | out_of_lib
    category: str
    series: str = ""
    spec_id: str = ""
    clause: str = ""
    section_title: str = ""
    excerpt: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)  # multi_section 用
    prompt_arg: str = ""  # negative 的 domain/area


def _pick_chunk(
    chunks: list[dict], chunk_type: str, used: set[str], rng: random.Random
) -> dict | None:
    cands = [
        c
        for c in chunks
        if str(c.get("chunk_type")) == chunk_type
        and c.get("chunk_id") not in used
        and _is_usable(c, chunk_type)
    ]
    if not cands:
        return None
    return rng.choice(cands)


def build_positive_jobs(
    slots: list[tuple[str, str]],
    by_series: dict[str, list[str]],
    by_spec_dir: Path,
    rng: random.Random,
) -> list[GenJob]:
    """把 (category, series) 槽位填成具体 chunk 出题任务；series 内无合适 chunk 则跨 series 兜底。"""  # noqa: E501
    jobs: list[GenJob] = []
    used: set[str] = set()
    cache: dict[str, list[dict]] = {}

    def chunks_of(spec: str) -> list[dict]:
        if spec not in cache:
            cache[spec] = load_spec_chunks(spec, by_spec_dir)
        return cache[spec]

    all_series = list(by_series.keys())
    for cat, series in slots:
        chunk_type = CATEGORY_CHUNK_TYPE[cat]
        # 优先本 series；不足则按 series 顺序兜底
        order = [series] + [s for s in all_series if s != series]
        job: GenJob | None = None
        for s in order:
            specs = by_series.get(s, [])[:]
            rng.shuffle(specs)
            for spec in specs:
                ch = chunks_of(spec)
                if cat == "multi_section":
                    job = _multi_section_job(spec, ch, used, rng)
                else:
                    picked = _pick_chunk(ch, chunk_type, used, rng)
                    if picked is not None:
                        used.add(picked["chunk_id"])
                        job = GenJob(
                            kind="positive",
                            category=cat,
                            series=s,
                            spec_id=spec,
                            clause=str(picked.get("clause") or ""),
                            section_title=str(picked.get("section_title") or ""),
                            excerpt=chunk_text(picked)[:MAX_EXCERPT_CHARS],
                        )
                if job is not None:
                    break
            if job is not None:
                break
        if job is not None:
            jobs.append(job)
    return jobs


def _multi_section_job(
    spec: str, chunks: list[dict], used: set[str], rng: random.Random
) -> GenJob | None:
    """同一 spec 取 2-3 段相邻 text chunk 拼成 multi_section 任务。"""
    texts = [
        c
        for c in chunks
        if str(c.get("chunk_type")) == "text"
        and c.get("chunk_id") not in used
        and _is_usable(c, "text")
    ]
    if len(texts) < 2:
        return None
    texts.sort(key=lambda c: int(c.get("document_order") or 0))
    start = rng.randint(0, max(0, len(texts) - 3))
    picked = texts[start : start + 3]
    if len(picked) < 2:
        return None
    for c in picked:
        used.add(c["chunk_id"])
    sections = [(str(c.get("clause") or ""), str(c.get("section_title") or "")) for c in picked]
    excerpt = "\n\n".join(chunk_text(c) for c in picked)[:MAX_EXCERPT_CHARS]
    return GenJob(
        kind="multi_section",
        category="multi_section",
        series=spec[:2],
        spec_id=spec,
        sections=sections,
        excerpt=excerpt,
    )


def build_negative_jobs(rng: random.Random, oversample: float) -> list[GenJob]:
    jobs: list[GenJob] = []
    n_fp = max(NEG_FALSE_PREMISE_TARGET, round(NEG_FALSE_PREMISE_TARGET * oversample))
    n_ol = max(NEG_OUT_OF_LIB_TARGET, round(NEG_OUT_OF_LIB_TARGET * oversample))
    for i in range(n_fp):
        jobs.append(
            GenJob(
                kind="false_premise",
                category="negative",
                prompt_arg=NEG_FALSE_PREMISE_DOMAINS[i % len(NEG_FALSE_PREMISE_DOMAINS)],
            )
        )
    for i in range(n_ol):
        jobs.append(
            GenJob(
                kind="out_of_lib",
                category="negative",
                prompt_arg=NEG_OUT_OF_LIB_AREAS[i % len(NEG_OUT_OF_LIB_AREAS)],
            )
        )
    return jobs


# === 校验 / 规范化 ========================================================


def validate_and_normalize(
    parsed: dict[str, Any], *, kind: str, whitelist: set[str]
) -> tuple[dict | None, str | None]:
    """LLM JSON → golden item dict 或 (None, skip_reason)。whitelist = R18 交集 spec 集合。"""
    if parsed.get("skip_reason"):
        return None, str(parsed["skip_reason"])[:200]

    is_negative = kind in ("false_premise", "out_of_lib")
    q = str(parsed.get("question") or "").strip()
    if not q:
        return None, "empty-question"
    if len(q) > MAX_QUESTION_CHARS:
        q = q[:MAX_QUESTION_CHARS] + "…"

    category = "negative" if is_negative else str(parsed.get("category") or "").strip().lower()
    if not is_negative and category not in POSITIVE_CATEGORY_TARGETS:
        return None, f"invalid-category: {category}"

    expected_specs: list[dict] = []
    if not is_negative:
        seen: set[str] = set()
        for s in parsed.get("expected_specs") or []:
            if not isinstance(s, dict):
                continue
            sid = normalize_spec_id(str(s.get("spec_id") or "")) or ""
            if sid not in whitelist or sid in seen:
                continue
            seen.add(sid)
            secs = s.get("sections") or []
            if isinstance(secs, str):
                secs = [secs]
            expected_specs.append(
                {"spec_id": sid, "sections": [str(x) for x in secs if str(x).strip()]}
            )
        if not expected_specs:
            return None, "no-whitelist-spec"

    facts_raw = parsed.get("expected_facts") or []
    facts = [str(f).strip()[:200] for f in facts_raw if str(f).strip()]
    if not is_negative and len(facts) < MIN_FACTS:
        return None, f"facts<{MIN_FACTS}"
    facts = facts[:MAX_FACTS]

    forbidden = [str(f).strip() for f in (parsed.get("forbidden") or []) if str(f).strip()][
        :MAX_FORBIDDEN
    ]

    notes = str(parsed.get("notes") or "").strip()[:300]

    item: dict = {
        "category": category,
        "language": "en",
        "question": q,
        "expected_specs": expected_specs,
        "expected_facts": facts,
        "forbidden": forbidden,
        "source": "hand_crafted",
    }
    if is_negative:
        item["must_say_not_found"] = True
    if notes:
        item["notes"] = notes
    return item, None


# === R18 公平核验 =========================================================


class B_R18_Corpus:
    """B 的 Release-18 全文（Documents.db）按 spec_id 懒加载 + 规范化缓存。"""

    def __init__(self, db_path: Path = B_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._norm_cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return self._db_path.is_file()

    def _conn_(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        return self._conn

    def normalized_text(self, spec_id: str) -> str:
        if spec_id in self._norm_cache:
            return self._norm_cache[spec_id]
        like = spec_id.replace(".", "") + "%"
        row = (
            self._conn_()
            .execute("select data from Standard where id like ? limit 1", (like,))
            .fetchone()
        )
        text = ""
        if row:
            try:
                text = _NONWORD_RE.sub(" ", json.loads(row[0]).get("text", "").lower())
            except Exception:
                text = ""
        self._norm_cache[spec_id] = text
        return text

    def fact_coverage(self, facts: list[str], spec_id: str) -> float:
        """expected_facts 在 B 的 R18 全文里的覆盖率（每条按 >=4 字符显著词 60% 命中算覆盖）。"""
        if not facts:
            return 1.0
        text = self.normalized_text(spec_id)
        if not text:
            return 0.0
        covered = 0
        for fact in facts:
            words = [w for w in _NONWORD_RE.sub(" ", fact.lower()).split() if len(w) >= 4]
            if not words:
                # 全短词（缩写/符号）：直接子串找规范化后的 fact
                if _NONWORD_RE.sub(" ", fact.lower()).strip() in text:
                    covered += 1
                continue
            hit = sum(1 for w in words if w in text)
            if hit / len(words) >= 0.6:
                covered += 1
        return covered / len(facts)


# === 异步生成 =============================================================


def _messages_for(job: GenJob) -> list[dict[str, str]]:
    if job.kind == "positive":
        from eval.huawei_compare.gen_prompts import CHUNK_TYPE_CATEGORIES

        ct = CATEGORY_CHUNK_TYPE[job.category]
        cats = CHUNK_TYPE_CATEGORIES.get(ct, (job.category,))
        return build_positive_messages(
            spec_id=job.spec_id,
            clause=job.clause,
            section_title=job.section_title,
            excerpt=job.excerpt,
            categories=cats,
            category_hint=job.category,
        )
    if job.kind == "multi_section":
        return build_multi_section_messages(
            spec_id=job.spec_id, sections=job.sections, excerpt=job.excerpt
        )
    if job.kind == "false_premise":
        return build_false_premise_messages(domain=job.prompt_arg)
    if job.kind == "out_of_lib":
        return build_out_of_lib_messages(area=job.prompt_arg)
    raise ValueError(f"unknown job kind: {job.kind}")


@dataclass(slots=True)
class GenResult:
    job: GenJob
    item: dict | None = None
    skip_reason: str | None = None
    error: str | None = None
    r18_coverage: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


async def _gen_one(
    job: GenJob,
    *,
    client: _LiteLLMChatClient,
    limiter: _RpmLimiter,
    sem: asyncio.Semaphore,
    whitelist: set[str],
    corpus: B_R18_Corpus,
) -> GenResult:
    messages = _messages_for(job)
    async with sem:
        await limiter.acquire()
        try:
            payload = await client.chat(
                messages=messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=0.4
            )
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
            return GenResult(job=job, error=f"{type(exc).__name__}: {exc}"[:200])

    content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = payload.get("usage") or {}
    pt, cot = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    try:
        parsed = _extract_json(content)
    except Exception as exc:
        return GenResult(
            job=job, error=f"json: {exc}"[:200], prompt_tokens=pt, completion_tokens=cot
        )

    item, skip = validate_and_normalize(parsed, kind=job.kind, whitelist=whitelist)
    if item is None:
        return GenResult(job=job, skip_reason=skip, prompt_tokens=pt, completion_tokens=cot)

    cov: float | None = None
    if job.kind in ("positive", "multi_section") and corpus.available:
        sid = item["expected_specs"][0]["spec_id"]
        cov = corpus.fact_coverage(item["expected_facts"], sid)
        if cov < R18_COVERAGE_MIN:
            return GenResult(
                job=job,
                skip_reason=f"low-r18-coverage:{cov:.2f}",
                r18_coverage=cov,
                prompt_tokens=pt,
                completion_tokens=cot,
            )
    return GenResult(job=job, item=item, r18_coverage=cov, prompt_tokens=pt, completion_tokens=cot)


async def generate(
    jobs: list[GenJob],
    *,
    client: _LiteLLMChatClient,
    whitelist: set[str],
    corpus: B_R18_Corpus,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
) -> list[GenResult]:
    limiter = _RpmLimiter(rpm)
    sem = asyncio.Semaphore(concurrent)
    t0 = time.perf_counter()
    log.info(
        "generate start: jobs=%d model=%s rpm=%d conc=%d", len(jobs), client.model, rpm, concurrent
    )
    tasks = [
        asyncio.create_task(
            _gen_one(j, client=client, limiter=limiter, sem=sem, whitelist=whitelist, corpus=corpus)
        )
        for j in jobs
    ]
    results: list[GenResult] = []
    for done, fut in enumerate(asyncio.as_completed(tasks), 1):
        results.append(await fut)
        if done % 25 == 0:
            ok = sum(1 for r in results if r.item)
            log.info(
                "  progress %d/%d  accepted=%d  (%.0fs)",
                done,
                len(jobs),
                ok,
                time.perf_counter() - t0,
            )
    log.info("generate done in %.0fs", time.perf_counter() - t0)
    return results


# === 选 100 + 写盘 ========================================================


def select_balanced(results: list[GenResult]) -> list[dict]:
    """按 category 目标配额 + series 轮转选齐；positive 优先高 R18 覆盖率。"""
    accepted = [r for r in results if r.item]
    by_cat: dict[str, list[GenResult]] = {}
    for r in accepted:
        by_cat.setdefault(r.item["category"], []).append(r)

    targets = dict(POSITIVE_CATEGORY_TARGETS)
    targets["negative"] = NEG_FALSE_PREMISE_TARGET + NEG_OUT_OF_LIB_TARGET
    chosen: list[dict] = []
    for cat, target in targets.items():
        pool = by_cat.get(cat, [])
        if cat == "negative":
            picked = _balance_negatives(pool, target)
        else:
            # 高覆盖率优先，再按 series 轮转保证分布
            pool = sorted(pool, key=lambda r: -(r.r18_coverage or 0))
            picked = _round_robin_by_series(pool, target)
        chosen.extend(p.item for p in picked)
    return chosen


def _balance_negatives(pool: list[GenResult], target: int) -> list[GenResult]:
    fp = [r for r in pool if r.job.kind == "false_premise"]
    ol = [r for r in pool if r.job.kind == "out_of_lib"]
    return fp[:NEG_FALSE_PREMISE_TARGET] + ol[:NEG_OUT_OF_LIB_TARGET]


def _round_robin_by_series(pool: list[GenResult], target: int) -> list[GenResult]:
    buckets: dict[str, list[GenResult]] = {}
    for r in pool:
        buckets.setdefault(r.job.series, []).append(r)
    order = sorted(buckets, key=lambda s: -POSITIVE_SERIES_QUOTA.get(s, 0))
    out: list[GenResult] = []
    while len(out) < target and any(buckets[s] for s in order):
        for s in order:
            if buckets[s]:
                out.append(buckets[s].pop(0))
                if len(out) >= target:
                    break
    return out


def _assign_ids(items: list[dict]) -> None:
    short = {
        "definition": "def",
        "procedure": "proc",
        "multi_section": "multi",
        "table_lookup": "table",
        "formula": "form",
        "negative": "neg",
    }
    counters: dict[str, int] = {}
    items.sort(key=lambda x: (x["category"], x["question"]))
    for it in items:
        c = it["category"]
        counters[c] = counters.get(c, 0) + 1
        it["id"] = f"hc-{short.get(c, 'qa')}-{counters[c]:03d}"


def write_golden(items: list[dict], out_path: Path) -> None:
    _assign_ids(items)
    doc = {
        "version": 1,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "total": len(items),
        "sources": ["hand_crafted"],
        "categories": sorted({i["category"] for i in items}),
        "items": items,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8"
    )
    log.info("wrote %s (n=%d)", out_path, len(items))


def _dump_artifacts(results: list[GenResult], out_dir: Path) -> dict:
    """skipped/failed jsonl + stats。返回 stats dict。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    skipped, failed = [], []
    pt = cot = 0
    by_cat: dict[str, int] = {}
    for r in results:
        pt += r.prompt_tokens
        cot += r.completion_tokens
        if r.item:
            by_cat[r.item["category"]] = by_cat.get(r.item["category"], 0) + 1
        elif r.error:
            failed.append({"kind": r.job.kind, "spec": r.job.spec_id, "error": r.error})
        else:
            skipped.append(
                {"kind": r.job.kind, "spec": r.job.spec_id, "skip_reason": r.skip_reason}
            )
    (out_dir / "gen_skipped.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in skipped), encoding="utf-8"
    )
    (out_dir / "gen_failed.jsonl").write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in failed), encoding="utf-8"
    )
    stats = {
        "total_jobs": len(results),
        "accepted": sum(1 for r in results if r.item),
        "skipped": len(skipped),
        "failed": len(failed),
        "accepted_by_category": dict(sorted(by_cat.items())),
        "prompt_tokens": pt,
        "completion_tokens": cot,
    }
    (out_dir / "gen_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


# === CLI ==================================================================


def build_client(settings: EvalSettings | None = None) -> _LiteLLMChatClient:
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise RuntimeError("LITELLM_API_KEY missing in env/.env")
    return _LiteLLMChatClient(
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        model=s.llm_agent_model,  # mimo-v2.5-pro
        timeout_s=DEFAULT_TIMEOUT_S,
    )


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    by_spec_dir = Path(args.by_spec_dir)
    if not by_spec_dir.is_dir():
        log.error("by_spec 目录不存在：%s（设 --by-spec-dir 或 INGEST_DATA_DIR）", by_spec_dir)
        return 2
    rng = random.Random(args.seed)
    specs = load_intersection_specs()
    whitelist = set(specs)  # expected_specs 校验用全交集
    sampling_specs = [s for s in specs if not is_excluded_spec(s)]  # 采样源排除测试/RF/study
    by_series = specs_by_series(sampling_specs)
    log.info(
        "采样源 spec: %d（交集 %d 减测试/RF/study %d）",
        len(sampling_specs),
        len(specs),
        len(specs) - len(sampling_specs),
    )

    slots = plan_slots(POSITIVE_CATEGORY_TARGETS, POSITIVE_SERIES_QUOTA, args.oversample)
    pos_jobs = build_positive_jobs(slots, by_series, by_spec_dir, rng)
    neg_jobs = build_negative_jobs(rng, args.oversample)
    jobs = pos_jobs + neg_jobs
    log.info("jobs: positive=%d negative=%d total=%d", len(pos_jobs), len(neg_jobs), len(jobs))

    corpus = B_R18_Corpus()
    if not corpus.available:
        log.warning("B R18 Documents.db 不存在(%s) → 跳过 R18 核验", B_DB_PATH)

    client = build_client(s)
    try:
        results = await generate(jobs, client=client, whitelist=whitelist, corpus=corpus)
    finally:
        await client.aclose()

    out_path = Path(args.out)
    stats = _dump_artifacts(results, out_path.parent)
    items = select_balanced(results)
    write_golden(items, out_path)
    # 候选全集（含被余出的）另存，便于人审替换
    cand = [r.item for r in results if r.item]
    _assign_ids(cand)
    write_golden(cand, out_path.with_suffix(".candidates.yaml"))
    log.info("stats: %s | selected=%d", stats, len(items))
    print(
        json.dumps(
            {**stats, "selected": len(items), "out": str(out_path)}, ensure_ascii=False, indent=2
        )
    )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="华为对比测试 100 题生成器")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--oversample", type=float, default=1.4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--by-spec-dir", default=str(default_a_dir()))
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
