"""ingestion 顶层 CLI 入口。

子命令分布（详见 docs/03-development/02-ingestion-and-indexing.md §4.6）：

  hf-pull / hf-audit / hf-load          主路径（GSMA HF）— 见 ingestion.hf_loader.runner
  chunk                                  chunker — 见 ingestion.chunker.runner
  vision-call / vision-cache             Vision pipeline — 见 ingestion.images.runner
  embed / index / pipeline-hf /          Indexer（Voyage/GLM → Qdrant + BM25 + PG）
  index-status / purge-spec              — 见 ingestion.indexer.runner
  glossary-extract                       术语抽取（M4.1）— 见 ingestion.glossary.runner

后续模块（parse-single / docling-fallback 等）按里程碑陆续接入；
本 CLI 文件只做命令注册，业务逻辑下沉到各子模块。
"""

from __future__ import annotations

import typer

from .chunker import runner as chunker_runner
from .glossary import runner as glossary_runner
from .hf_loader import runner as hf_runner
from .images import runner as vision_runner
from .indexer import runner as indexer_runner

app = typer.Typer(no_args_is_help=True, help="3GPP-Everything ingestion CLI")

# 直接复用各子模块 runner 中已注册的子命令（保持 `ingestion hf-pull` 风格）
for command_info in hf_runner.app.registered_commands:
    app.registered_commands.append(command_info)
for command_info in chunker_runner.app.registered_commands:
    app.registered_commands.append(command_info)
for command_info in vision_runner.app.registered_commands:
    app.registered_commands.append(command_info)
for command_info in indexer_runner.app.registered_commands:
    app.registered_commands.append(command_info)
for command_info in glossary_runner.app.registered_commands:
    app.registered_commands.append(command_info)


if __name__ == "__main__":
    app()
