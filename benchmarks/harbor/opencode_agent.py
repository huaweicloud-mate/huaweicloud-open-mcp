"""Harbor agent 适配：把 opencode CLI 包装为 Harbor solver（M7 薄胶水）。

加载方式（harbor 运行环境，py>=3.12 且已安装 harbor）：

    cd <项目根>
    harbor run -p datasets/mcp-regression/<case_id> \
        --agent benchmarks.harbor.opencode_agent:OpencodeAgent \
        --model <provider/model>

本项目不声明 harbor 依赖（harbor 0.19 要求 py>=3.12，与本仓库 >=3.10 冲突）：
基类由 harbor 运行时提供，本文件只在 harbor 进程内被 import。装配约定
（MCP 命令 / audit 文件 / 权限预批）的单一真值源在 conventions.py，
由宿主侧单测锚定；install/run 的全部命令经由 BaseInstalledAgent 的 exec helpers。
"""

import base64
import json
import shlex
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template  # noqa: E402

from benchmarks.harbor import conventions as conv  # noqa: E402

OPENCODE_VERSION = "1.18.23"


class OpencodeAgent(BaseInstalledAgent):
    @staticmethod
    def name() -> str:
        return "opencode"

    def version(self) -> str | None:
        return OPENCODE_VERSION

    async def install(self, environment) -> None:
        # opencode CLI（官方安装脚本）+ 预批权限的 opencode.json（约定见 conventions）
        await self.exec_as_root(environment, command=(
            "curl -fsSL https://opencode.ai/install | bash"))
        encoded = base64.b64encode(
            json.dumps(conv.build_agent_opencode_config(),
                       ensure_ascii=False).encode("utf-8")).decode("ascii")
        await self.exec_as_agent(environment, command=(
            f"mkdir -p {shlex.quote(conv.AGENT_WORKDIR)} /tmp"
            f" && echo {encoded} | base64 -d > {conv.AGENT_WORKDIR}/opencode.json"))

    @with_prompt_template
    async def run(self, instruction, environment, context) -> None:
        await self.exec_as_agent(environment, command=(
            f"opencode run {shlex.quote(instruction)} --format json"
            f" --dir {shlex.quote(conv.AGENT_WORKDIR)}"))

    def populate_context_post_run(self, context) -> None:
        """尽力提取 opencode 输出进 context（trial 奖励以 verifier 为准）。"""
        for attr in ("output", "stdout", "result", "logs"):
            raw = getattr(context, attr, None) or getattr(self, f"_{attr}", None)
            if isinstance(raw, str) and raw.strip():
                try:
                    context.external_metadata |= {"opencode_output_tail": raw[-4000:]}
                except Exception:
                    pass
                return
