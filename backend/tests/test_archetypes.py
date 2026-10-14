"""archetypes 测试：时长缩放守恒、桶配比规范化终止性与不变量。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.archetypes import (
    ARCHETYPES,
    MIN_BUCKET_MINUTES,
    normalize_mix,
    rebalance_course_buckets,
    scale_to_duration,
    select_archetype,
)


def test_scale_preserves_total_duration():
    for key, arch in ARCHETYPES.items():
        for duration in (120, 180, 240, 360, 480, 720):
            scaled = scale_to_duration(arch, duration)
            total = sum(m["minutes"] for m in scaled.modules)
            assert total == duration, f"{key}@{duration}: {total}"
            for m in scaled.modules:
                assert m["minutes"] >= 15
                assert m["minutes"] % 15 == 0
                assert sum(m["bucket_mix"].values()) == m["minutes"]


def test_normalize_mix_terminates_on_all_small_equal_buckets():
    # 回归：曾因过期快照值 + 向零桶折叠导致 (5,5,5) 死循环
    out = normalize_mix({"formal": 5, "social": 5, "practice": 5})
    assert sum(out.values()) == 15
    assert all(v == 0 or v >= MIN_BUCKET_MINUTES for v in out.values())


def test_normalize_mix_folds_small_into_largest():
    # 语义：小于 min 的桶折叠进最大非零桶（而非补足），总和守恒
    out = normalize_mix({"formal": 7, "social": 3, "practice": 20})
    assert out == {"formal": 0, "social": 0, "practice": 30}
    assert sum(out.values()) == 30


def test_normalize_mix_keeps_valid_mix_unchanged():
    mix = {"formal": 20, "social": 15, "practice": 25}
    assert normalize_mix(mix) == mix


def test_normalize_mix_brute_force_terminates():
    for total in range(15, 95, 5):
        for f in range(0, total + 1, 5):
            for s in range(0, total - f + 1, 5):
                p = total - f - s
                out = normalize_mix({"formal": f, "social": s, "practice": p})
                assert sum(out.values()) == total
                assert all(v == 0 or v >= MIN_BUCKET_MINUTES for v in out.values())


def test_select_archetype_by_keywords():
    assert select_archetype("新任管理者转型", "埋头做事", "").key == "new_manager_transition"
    assert select_archetype(None, "跨部门推不动", "").key == "generic_leadership"
    assert select_archetype(None, "战略共识缺失", "").key == "strategy_alignment_workshop"


# ---------------------------------------------------------------------------
# 课程级配比修复（回归：180 分钟短课程曾因小桶折叠导致人际占比塌到 8%）
# ---------------------------------------------------------------------------
def _course_modules(duration: int, archetype_key: str = "new_manager_transition") -> list[dict]:
    scaled = scale_to_duration(ARCHETYPES[archetype_key], duration)
    mods = []
    for m in scaled.modules:
        mix = normalize_mix(m["bucket_mix"])
        mods.append({"title": m["title"], "minutes": m["minutes"], "bucket_mix": mix})
    return mods


def _shares(modules: list[dict]) -> dict[str, float]:
    totals = {"formal": 0, "social": 0, "practice": 0}
    for m in modules:
        for k, v in m["bucket_mix"].items():
            totals[k] += v
    total = sum(totals.values())
    return {k: v / total for k, v in totals.items()}


def test_rebalance_repairs_short_course_social_deficit():
    modules = _course_modules(180)
    before = _shares(modules)
    assert before["social"] < 0.20, "前置条件：未修复时人际占比应低于下限"
    rebalance_course_buckets(modules)
    after = _shares(modules)
    assert after["social"] >= 0.20
    assert after["practice"] >= 0.30
    assert after["formal"] <= 0.50


def test_rebalance_preserves_invariants_across_durations():
    for key in ARCHETYPES:
        for duration in (120, 180, 240, 360, 480, 720):
            modules = _course_modules(duration, key)
            total_before = sum(m["minutes"] for m in modules)
            rebalance_course_buckets(modules)
            share = _shares(modules)
            for m in modules:
                assert sum(m["bucket_mix"].values()) == m["minutes"], (key, duration, m)
                for k, v in m["bucket_mix"].items():
                    assert v == 0 or v >= MIN_BUCKET_MINUTES, (key, duration, m["title"], k, v)
            assert sum(m["minutes"] for m in modules) == total_before
            assert share["social"] >= 0.20, (key, duration, share)
            assert share["practice"] >= 0.30, (key, duration, share)
            assert share["formal"] <= 0.50, (key, duration, share)


def test_rebalance_noop_when_already_compliant():
    modules = [{"title": "M", "minutes": 100, "bucket_mix": {"formal": 30, "social": 30, "practice": 40}}]
    rebalance_course_buckets(modules)
    assert modules[0]["bucket_mix"] == {"formal": 30, "social": 30, "practice": 40}
