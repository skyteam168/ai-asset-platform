"""llm 节点调用的接口。LLM Gateway（architecture.md 8.6）是独立模块，这里只定义引擎依赖的最小接口。

引擎对 LLM 的约束（与 Gateway 实现无关）：输出必须通过节点声明的 output_schema 校验，
只保留 Schema 声明的字段；LLM 节点在 DSL 里没有分支字段，输出只能作为数据被后续节点读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aip.libs.common.errors import ErrorCode, PlatformError


@dataclass
class LlmResult:
    data: Any
    tokens: int = 0


class LlmClient(Protocol):
    async def complete(
        self,
        *,
        model_profile: str,
        prompt: str,
        variables: dict[str, Any],
        output_schema: dict[str, Any],
        company_id: str,
    ) -> LlmResult: ...


class UnconfiguredLlm:
    async def complete(self, **_: Any) -> LlmResult:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM Gateway 未配置")
