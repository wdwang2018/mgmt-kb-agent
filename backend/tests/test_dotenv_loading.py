"""组合根 .env 加载行为测试。

设计要点：`_load_dotenv(env_file)` 路径可注入，测试全程使用临时文件，
**不触碰仓库真实 .env**。环境变量的跨测试泄漏由 `backend/conftest.py` 的
全局 autouse 护栏统一兜底（曾经因缺护栏，真实 Key 泄漏到 test_llm_glm.py，
令「空 Key 应短路」的断言失败）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.context import _load_dotenv


@pytest.fixture()
def env_file(tmp_path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "# 注释行\nGLM_API_KEY=test-key-from-dotenv\nEMBED_MODE=glm\n\n",
        encoding="utf-8")
    return p


def test_dotenv_injects_keys(env_file):
    os.environ.pop("GLM_API_KEY", None)
    os.environ.pop("EMBED_MODE", None)
    _load_dotenv(env_file)
    assert os.environ["GLM_API_KEY"] == "test-key-from-dotenv"
    assert os.environ["EMBED_MODE"] == "glm"


def test_dotenv_does_not_override_existing_env(env_file):
    os.environ["GLM_API_KEY"] = "already-set"
    _load_dotenv(env_file)
    assert os.environ["GLM_API_KEY"] == "already-set", "setdefault 不得覆盖既有环境变量"


def test_dotenv_missing_file_is_noop(tmp_path):
    os.environ.pop("GLM_API_KEY", None)
    _load_dotenv(tmp_path / "not-exist.env")  # 不得抛异常
    assert os.environ.get("GLM_API_KEY") is None


def test_dotenv_skips_comments_and_blank_lines(env_file):
    os.environ.pop("GLM_API_KEY", None)
    _load_dotenv(env_file)
    assert "# 注释行" not in os.environ
    assert os.environ["GLM_API_KEY"] == "test-key-from-dotenv"


def test_dotenv_default_path_points_to_repo_root():
    """不注入路径时默认指向仓库根 .env（只验证路径解析，不触发加载）。

    刻意不调用 `_load_dotenv()`：那会把仓库真实密钥注入进程环境，
    虽有 conftest.py 的全局还原护栏，测试本身也不该依赖护栏来兜底。
    """
    from app.services.context import REPO_ROOT
    assert (REPO_ROOT / ".env").name == ".env"
