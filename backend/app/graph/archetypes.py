"""课程原型（Archetype）：P0 两个主题的模块时间轴模板。

设计依据：
- 管理培训是问题驱动的，两个高频主题各沉淀一套「默认骨架」，
  具体模块的理论内容由内容匹配节点按「能力域 × 困境」检索填充；
- 时间轴到 15 分钟粒度，70-20-10 配比在骨架层面即保证合规（G3 门禁兜底）。
"""

from __future__ import annotations

from ..domain.constants import DEFAULT_70_20_10_RULE
from ..domain.models import Bucket


class Archetype:
    def __init__(self, key: str, title: str, dilemma_tags: list[str],
                 modules: list[dict], positioning: str):
        self.key = key
        self.title = title
        self.dilemma_tags = dilemma_tags
        self.modules = modules  # [{title, minutes, focus_domains, focus_dilemmas, bucket_mix}]
        self.positioning = positioning


def _m(title: str, minutes: int, domains: list[str], dilemmas: list[str],
       formal: int, social: int, practice: int) -> dict:
    return {
        "title": title, "minutes": minutes,
        "focus_domains": domains, "focus_dilemmas": dilemmas,
        "bucket_mix": {"formal": formal, "social": social, "practice": practice},
    }


NEW_MANAGER_TRANSITION = Archetype(
    key="new_manager_transition",
    title="新任管理者转身",
    dilemma_tags=["D01", "D02", "D17", "D09"],
    positioning="帮助新任经理完成从个人贡献者到团队领导者的角色转身，建立授权、反馈与 1on1 的基本管理节奏",
    modules=[
        _m("开场：转身之痛——为什么最能干的人反而当不好经理", 45, ["TEAM"], ["D01"], 20, 10, 15),
        _m("模块一：角色转变——管理者的成果口径是什么", 90, ["TEAM", "EXEC"], ["D01", "D17"], 30, 20, 40),
        _m("模块二：授权——五级阶梯与三要素", 90, ["TEAM", "EXEC"], ["D02"], 25, 20, 45),
        _m("模块三：反馈与辅导——SBI 与 GROW", 90, ["COACH", "COMM"], ["D10", "D09"], 25, 25, 40),
        _m("模块四：团队与 1on1——节奏即管理", 75, ["TEAM", "COACH"], ["D03", "D12"], 20, 20, 35),
        _m("演练：真实情境角色扮演（分组）", 90, ["TEAM", "COMM", "COACH"], ["D02", "D09"], 5, 20, 65),
        _m("复盘与行动计划：我的转身 90 天清单", 60, ["EXEC"], ["D17"], 15, 25, 20),
    ],
)

STRATEGY_ALIGNMENT_WORKSHOP = Archetype(
    key="strategy_alignment_workshop",
    title="战略共识与解码工作坊",
    dilemma_tags=["D07", "D08", "D06", "D16"],
    positioning="把战略主张翻译成各层级可执行的仗与承诺，建立言-行-考一致的共识机制",
    modules=[
        _m("开场：都同意的战略会，散会后各干各的", 30, ["STRAT", "COMM"], ["D07"], 15, 5, 10),
        _m("模块一：什么是好战略——诊断-指导方针-连贯行动", 60, ["STRAT"], ["D06"], 30, 15, 15),
        _m("模块二：战略解码——从战略主张到我的仗", 90, ["STRAT", "EXEC"], ["D08", "D16"], 25, 25, 40),
        _m("模块三：共识验证——言-行-考三层对齐", 60, ["STRAT", "COMM"], ["D07"], 20, 20, 20),
        _m("模块四：工具选型——BSC/OGSM/OKR 的情境分工", 60, ["STRAT", "EXEC"], ["D16"], 25, 15, 20),
        _m("共创：世界咖啡三轮——我的仗与需要的支持", 90, ["STRAT", "COMM", "CHANGE"], ["D07", "D04"], 5, 25, 60),
        _m("收敛：承诺清单与复盘机制设计", 45, ["EXEC", "STRAT"], ["D08"], 10, 15, 20),
    ],
)

GENERIC_LEADERSHIP = Archetype(
    key="generic_leadership",
    title="管理者能力提升",
    dilemma_tags=["D01", "D03", "D15", "D17"],
    positioning="围绕真实管理困境，完成从认知到行为改变的闭环训练",
    modules=[
        _m("开场：困境导入——学员自带案例", 30, ["TEAM"], ["D01"], 10, 10, 10),
        _m("模块一：管理基本功——角色与时间结构", 75, ["TEAM", "EXEC"], ["D01", "D17"], 30, 20, 25),
        _m("模块二：团队与协同——冲突化解", 75, ["TEAM", "COMM"], ["D03", "D04"], 25, 25, 25),
        _m("模块三：教练与反馈", 75, ["COACH", "COMM"], ["D10"], 25, 25, 25),
        _m("演练与复盘：情境角色扮演 + 行动计划", 90, ["TEAM", "COMM", "COACH"], ["D03", "D10"], 5, 25, 60),
    ],
)

ARCHETYPES: dict[str, Archetype] = {
    a.key: a for a in (NEW_MANAGER_TRANSITION, STRATEGY_ALIGNMENT_WORKSHOP, GENERIC_LEADERSHIP)
}


def select_archetype(theme_hint: str | None, pain: str, desired: str) -> Archetype:
    """按主题线索与痛点关键词选择课程原型。"""
    text = f"{theme_hint or ''} {pain} {desired}"
    if any(k in text for k in ["新任", "新经理", "晋升", "转身", "转型", "从技术到管理"]):
        return NEW_MANAGER_TRANSITION
    if any(k in text for k in ["战略", "共识", "解码", "目标对齐", "落地", "打仗"]):
        return STRATEGY_ALIGNMENT_WORKSHOP
    return GENERIC_LEADERSHIP


def scale_to_duration(archetype: Archetype, duration_minutes: int) -> Archetype:
    """将原型时间轴缩放到需求时长（保持 15 分钟粒度与桶比例，总量严格等于需求时长）。"""
    total = sum(m["minutes"] for m in archetype.modules)
    if total == duration_minutes:
        return archetype
    ratio = duration_minutes / total
    minutes_list = [max(15, round(m["minutes"] * ratio / 15) * 15) for m in archetype.modules]
    # 总量校正：把舍入漂移补到/扣自最大的模块，保证总和 == duration_minutes
    drift = duration_minutes - sum(minutes_list)
    if drift:
        i_max = minutes_list.index(max(minutes_list))
        minutes_list[i_max] = max(15, minutes_list[i_max] + drift)
    scaled = []
    for m, minutes in zip(archetype.modules, minutes_list):
        mix_total = sum(m["bucket_mix"].values()) or minutes
        mix = {k: round(v * minutes / mix_total) for k, v in m["bucket_mix"].items()}
        drift_m = minutes - sum(mix.values())
        if drift_m:
            biggest = max(mix, key=mix.get)
            mix[biggest] += drift_m
        scaled.append({**m, "minutes": minutes, "bucket_mix": mix})
    return Archetype(archetype.key, archetype.title, archetype.dilemma_tags, scaled, archetype.positioning)


BUCKET_NAMES = {Bucket.FORMAL.value: "正式学习", Bucket.SOCIAL.value: "人际学习", Bucket.PRACTICE.value: "实践学习"}

# 模块内桶的最小承接时长：低于该值的桶折叠进最大桶，
# 保证「模块每一分钟都有对应教学活动」（G3 按活动时长评估，不能被静默丢弃）。
MIN_BUCKET_MINUTES = 10


def normalize_mix(mix: dict[str, int], min_minutes: int = MIN_BUCKET_MINUTES) -> dict[str, int]:
    """把模块桶配比规范化：0 < 桶时长 < min_minutes 的折叠进最大的非零桶。

    正确性要点（曾在此处踩坑）：
    - 每轮重新读取当前值，不用迭代中的过期快照（否则会过度折叠）；
    - 只并入「非零」桶；若没有可并入的非零桶则保留残余并停止（总量本身就小的退化情形）；
    - 每轮至少清零一个小桶，轮数以桶数为上界，保证终止。
    """
    mix = dict(mix)
    for _ in range(len(mix) + 1):
        small = [k for k, v in mix.items() if 0 < v < min_minutes]
        if not small:
            break
        b = small[0]
        others = [k for k in mix if k != b and mix[k] > 0]
        if not others:
            break
        biggest = max(others, key=lambda k: mix[k])
        mix[biggest] += mix[b]
        mix[b] = 0
    return mix


def rebalance_course_buckets(modules: list[dict], rule: dict[str, float] | None = None,
                             max_rounds: int = 64) -> list[dict]:
    """课程级 70-20-10 配比修复（原地修改各模块的 bucket_mix，模块总时长不变）。

    背景：scale_to_duration + normalize_mix 在短课程上会把小模块的社交/正式桶
    折叠掉，课程级占比随之失真（实测 180 分钟课程人际占比塌到 8%，G3 拒收）。
    本函数在模块间搬运 15 分钟块，把课程级占比搬回规则区间。

    不变量（搬运后依然成立，保证活动生成节点不会丢桶）：
    - 每个非零桶 ≥ MIN_BUCKET_MINUTES（捐赠桶要么保留 ≥10，要么清零为 0）；
    - 每个模块的 minutes == sum(bucket_mix)（同模块内搬运，总量守恒）。
    """
    rule = rule or DEFAULT_70_20_10_RULE
    for _ in range(max_rounds):
        totals = {"formal": 0, "social": 0, "practice": 0}
        for m in modules:
            for k, v in m["bucket_mix"].items():
                totals[k] += v
        total = sum(totals.values())
        if total == 0:
            return modules
        share = {k: v / total for k, v in totals.items()}

        deficit = surplus = None
        if share["social"] < rule["social_min"]:
            deficit = "social"
        elif share["practice"] < rule["practice_min"]:
            deficit = "practice"
        if share["formal"] > rule["formal_max"]:
            surplus = "formal"
        if deficit is None and surplus is None:
            return modules  # 已满足规则
        if surplus is None:  # 只有下限未达：从占比最大的桶搬
            surplus = max(share, key=share.get)
            if surplus == deficit:
                return modules  # 无搬运空间，交门禁/HITL
        if deficit is None:  # 只有正式超上限：搬给离下限更远的桶
            deficit = "social" if share["social"] <= share["practice"] else "practice"

        donor = max((m for m in modules if m["bucket_mix"].get(surplus, 0) >= MIN_BUCKET_MINUTES),
                    key=lambda m: m["bucket_mix"].get(surplus, 0), default=None)
        if donor is None:
            return modules  # 余量不足（每模块该桶都 <10 分钟），交门禁/HITL
        mix = donor["bucket_mix"]
        chunk = 15 if mix[surplus] >= 25 else mix[surplus]
        mix[surplus] -= chunk
        mix[deficit] = mix.get(deficit, 0) + chunk
    return modules
