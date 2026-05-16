"""TeleQnA 数据拉取：clone GitHub repo + 解压加密 zip + 解析为 jsonl。

设计：
- 默认 clone 到 `eval/teleqna/data/repo/`（gitignore 掉，不进项目仓库）
- 如果已有 repo → `git pull` 更新；如已有 raw.jsonl 且 --skip-existing → 直接复用
- 解压 `TeleQnA.zip`（密码 'teleqnadataset'，WinZip AES = compression method 99；
  Python 内置 zipfile 与系统 `unzip` 都不支持，必须用 pyzipper）→ `TeleQnA.txt`
- 解析 dict 形式（key = "question 1" .. "question N"）→ list[dict] → jsonl
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pyzipper

log = logging.getLogger(__name__)

DEFAULT_REPO_URL = "https://github.com/netop-team/TeleQnA.git"
DEFAULT_DATA_DIR = Path(__file__).parent / "data"
DEFAULT_REPO_DIR = DEFAULT_DATA_DIR / "repo"
# TeleQnA repo 内 zip 文件名（实际为 TeleQnA.zip，README 描述里写 TeleQnA.txt.zip 是早期）
DEFAULT_ZIP_CANDIDATES = ("TeleQnA.zip", "TeleQnA.txt.zip")
DEFAULT_TXT_NAME = "TeleQnA.txt"
DEFAULT_RAW_JSONL = DEFAULT_DATA_DIR / "raw.jsonl"
ZIP_PASSWORD = b"teleqnadataset"


def clone_or_update(
    *,
    repo_url: str = DEFAULT_REPO_URL,
    repo_dir: Path = DEFAULT_REPO_DIR,
    timeout_s: int = 120,
) -> Path:
    """clone（首次）或 git pull（已存在）。返回 repo_dir。"""
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if (repo_dir / ".git").exists():
        log.info("teleqna repo exists, git pull → %s", repo_dir)
        subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            check=True,
            timeout=timeout_s,
            capture_output=True,
        )
    else:
        log.info("cloning teleqna: %s → %s", repo_url, repo_dir)
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(repo_dir)],
            check=True,
            timeout=timeout_s,
            capture_output=True,
        )
    return repo_dir


def extract_txt(*, repo_dir: Path = DEFAULT_REPO_DIR, dest_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """解压 TeleQnA[.txt].zip → TeleQnA.txt（用 pyzipper，支持 WinZip AES）。"""
    zip_path: Path | None = None
    for name in DEFAULT_ZIP_CANDIDATES:
        candidate = repo_dir / name
        if candidate.exists():
            zip_path = candidate
            break
    if zip_path is None:
        raise FileNotFoundError(
            f"TeleQnA zip not found in {repo_dir}; "
            f"tried {DEFAULT_ZIP_CANDIDATES}; repo layout may have changed"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    txt_path = dest_dir / DEFAULT_TXT_NAME
    with pyzipper.AESZipFile(zip_path) as zf:
        names = zf.namelist()
        if DEFAULT_TXT_NAME in names:
            txt_in_zip = DEFAULT_TXT_NAME
        else:
            txt_in_zip = next((n for n in names if n.endswith(DEFAULT_TXT_NAME)), None)
            if not txt_in_zip:
                raise RuntimeError(f"{DEFAULT_TXT_NAME} not in zip; got {names}")
        zf.setpassword(ZIP_PASSWORD)
        txt_path.write_bytes(zf.read(txt_in_zip))
    log.info(
        "extracted %s from %s → %s (%d bytes)",
        DEFAULT_TXT_NAME,
        zip_path.name,
        txt_path,
        txt_path.stat().st_size,
    )
    return txt_path


def parse_to_jsonl(
    txt_path: Path,
    out_path: Path = DEFAULT_RAW_JSONL,
) -> int:
    """解析 TeleQnA.txt（实为 JSON 顶层 dict） → jsonl。返回写入条数。

    入口结构（来自 README）：
      {
        "question 1": {
          "question": "...",
          "option 1": "...", "option 2": "...", ...,
          "answer": "option N: ...",
          "explanation": "...",
          "category": "Standards specifications"
        },
        "question 2": { ... },
        ...
      }
    """
    raw = json.loads(txt_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected TeleQnA root type: {type(raw).__name__}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as f:
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            record = {"id": key, **item}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    log.info("parsed %d items → %s", n, out_path)
    return n


def pull_all(
    *,
    repo_url: str = DEFAULT_REPO_URL,
    data_dir: Path = DEFAULT_DATA_DIR,
    skip_existing: bool = True,
) -> dict[str, Path | int]:
    """端到端：clone → extract → parse。返回路径与计数报告。"""
    repo_dir = data_dir / "repo"
    raw_jsonl = data_dir / "raw.jsonl"
    if skip_existing and raw_jsonl.exists() and raw_jsonl.stat().st_size > 0:
        n_existing = sum(1 for _ in raw_jsonl.open("r", encoding="utf-8"))
        log.info("raw.jsonl exists (%d items) → skip pull/extract/parse", n_existing)
        return {"raw_jsonl": raw_jsonl, "items": n_existing, "skipped": 1}

    clone_or_update(repo_url=repo_url, repo_dir=repo_dir)
    txt_path = extract_txt(repo_dir=repo_dir, dest_dir=data_dir)
    n = parse_to_jsonl(txt_path, out_path=raw_jsonl)
    return {"raw_jsonl": raw_jsonl, "items": n, "skipped": 0}


def cleanup_repo(repo_dir: Path = DEFAULT_REPO_DIR) -> None:
    """删除已克隆的 repo（解压完 raw.jsonl 之后可调，省 ~10MB 磁盘）。"""
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
        log.info("removed teleqna repo: %s", repo_dir)


__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_RAW_JSONL",
    "DEFAULT_REPO_DIR",
    "DEFAULT_REPO_URL",
    "ZIP_PASSWORD",
    "cleanup_repo",
    "clone_or_update",
    "extract_txt",
    "parse_to_jsonl",
    "pull_all",
]
