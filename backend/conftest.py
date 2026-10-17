"""全局测试护栏。

存在原因：组合根 `_load_dotenv()` 会把仓库 .env 的真实密钥注入 `os.environ`
（setdefault 语义）。此前没有护栏时，某个测试触发加载后，真实 Key 会泄漏到
同会话后续测试文件，导致 `test_llm_glm.py::test_glm_missing_key_short_circuits`
（断言空 Key 应短路）拿到真 Key 而失败——**测试间通过进程环境互相污染**。

本 fixture 对每个测试快照并还原整个环境变量，杜绝此类跨文件泄漏。

另设 `_isolate_checkpoint`：把 CHECKPOINT_MODE 默认压成 memory，防止 90+ 个
构建 AppContext 的测试把检查点写进仓库级 backend/checkpoints.db（与正在运行的
开发服务争锁）；需要验证 sqlite 持久化的测试自行 monkeypatch.setenv 覆盖
（autouse fixture 先于测试内请求的 fixture 装配，覆盖顺序确定）。
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


@pytest.fixture(autouse=True)
def _isolate_checkpoint(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_MODE", "memory")
