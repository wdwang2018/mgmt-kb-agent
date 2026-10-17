"""全局测试护栏。

存在原因：组合根 `_load_dotenv()` 会把仓库 .env 的真实密钥注入 `os.environ`
（setdefault 语义）。此前没有护栏时，某个测试触发加载后，真实 Key 会泄漏到
同会话后续测试文件，导致 `test_llm_glm.py::test_glm_missing_key_short_circuits`
（断言空 Key 应短路）拿到真 Key 而失败——**测试间通过进程环境互相污染**。

本 fixture 对每个测试快照并还原整个环境变量，杜绝此类跨文件泄漏。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_environ():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
