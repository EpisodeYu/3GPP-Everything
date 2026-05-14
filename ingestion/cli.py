"""ingestion 顶层 CLI 入口。

子命令分布（详见 docs/03-development/02-ingestion-and-indexing.md §4.6）：

  hf-pull / hf-audit / hf-load   主路径（GSMA HF）— 见 ingestion.hf_loader.runner

后续模块（chunk / embed / index / pipeline-hf / parse-single / status / purge）
按里程碑陆续接入，本 CLI 文件只做命令注册，业务逻辑下沉到各子模块。
"""

from __future__ import annotations

import typer

from .hf_loader import runner as hf_runner

app = typer.Typer(no_args_is_help=True, help="3GPP-Everything ingestion CLI")

# 直接复用 hf_loader.runner 中已注册的子命令（保持 `ingestion hf-pull` 风格）
for command_info in hf_runner.app.registered_commands:
    app.registered_commands.append(command_info)


if __name__ == "__main__":
    app()
