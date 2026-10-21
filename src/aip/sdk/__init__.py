"""官方 Python SDK（architecture.md 10.5）：数据面 REST + Skill 目录 + 可选 LangChain 适配。"""

from aip.sdk.client import Client
from aip.sdk.errors import SdkError
from aip.sdk.skills import SkillRegistryClient

__all__ = ["Client", "SdkError", "SkillRegistryClient"]
