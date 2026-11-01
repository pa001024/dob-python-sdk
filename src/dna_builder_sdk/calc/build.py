"""BD 装配（wasm 后端）：纯 BD JSON + 原始表 → 轻量 state。

计算（装配/属性/面板/技能结算）全部走内嵌 wasm（见 `calc/wasm.py`），
本模块只做 wasm 求值之外的轻量数据 plumbing：技能等级 trio 归一化、
溯源加成解析、最终技能等级装配。实体等级化（level_*）已随原生引擎移除。
"""

from __future__ import annotations

import math
import re as _re

TRACE_LEVEL_RE = _re.compile(r"\[([^\]]+)\]\s*等级\s*\+\s*(\d+)")


def _js_round_half_up(x: float) -> int:
    """复刻 Math.round（half-up；Python round 为 banker's）。"""
    return math.floor(float(x) + 0.5)


def normalize_skill_levels(value) -> list:
    """复刻 normalizeCharSkillLevels：数字→三元组；数组缺项沿首项补齐；各项取整钳制 1-12。"""

    def clamp(level, fallback: int) -> int:
        if not isinstance(level, (int, float)) or isinstance(level, bool) or not math.isfinite(level):
            return fallback
        return max(1, min(12, _js_round_half_up(level)))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        level = clamp(value, 10)
        return [level, level, level]
    if isinstance(value, list):
        first = clamp(value[0] if len(value) > 0 else None, 10)
        levels = [clamp(value[i] if i < len(value) else None, first) for i in range(3)]
        for i in range(3):
            item = value[i] if i < len(value) else None
            if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
                levels[i] = first if i == 0 else levels[0]
        return levels
    return [10, 10, 10]


def resolve_skill_level(levels, index: int) -> int:
    """复刻 resolveCharSkillLevel：0→E，1→Q，2 及之后→被动（非整数/≤0 按 E）。"""
    normalized = normalize_skill_levels(levels)
    if not isinstance(index, int) or isinstance(index, bool) or index <= 0:
        return normalized[0]
    if index == 1:
        return normalized[1]
    return normalized[2]


def parse_trace_levels(trace_texts: list, level: int) -> dict:
    """解析已解锁溯源节点的技能等级加成（TS 侧无此口径，为 SDK 新增能力）。

    :param trace_texts: 角色 溯源 文本列表（7 节点）
    :param level: 溯源等级（单数；N 即包含 1-N 所有节点的效果，超界钳制）
    :returns: {技能名: 累计加成}
    """
    try:
        count = max(0, min(len(trace_texts or []), int(level or 0)))
    except (TypeError, ValueError):
        return {}
    bonus: dict = {}
    for index in range(count):
        for name, amount in TRACE_LEVEL_RE.findall(trace_texts[index] or ""):
            bonus[name] = bonus.get(name, 0) + int(amount)
    return bonus


def _raw_char_skill_names(tables, char_id: int) -> tuple[list, list]:
    """角色原始技能名 + 溯源文本（缺失角色时抛 KeyError，与旧口径一致）。"""
    char_raw = tables.char_by_id.get(char_id)
    if char_raw is None:
        raise KeyError(f'角色 "{char_id}" 未在静态表中找到')
    skills = char_raw.get("技能") or []
    return [s.get("名称") for s in skills if isinstance(s, dict)], list(char_raw.get("溯源") or [])


def build_state(char_id: int, settings: dict, tables, skill_levels: list | None = None, traces: int | None = None) -> dict:
    """装配一次构筑的轻量 state（wasm 求值 + 最终技能等级用）。

    :param skill_levels: 技能等级（数字或 trio 数组；归一化规则同 normalizeCharSkillLevels）
    :param traces: 溯源等级（单数；7 即含 1-7 节点效果），缺省取 settings["traces"]（缺省 0）
    """
    settings = dict(settings or {})
    raw_levels = skill_levels if skill_levels is not None else settings.get("charSkillLevel", 10)
    trio = normalize_skill_levels(raw_levels)
    skill_names, trace_texts = _raw_char_skill_names(tables, char_id)
    trace_level = traces if traces is not None else settings.get("traces", 0)
    trace_bonus = parse_trace_levels(trace_texts, trace_level if trace_level is not None else 0)
    finals = []
    for i, name in enumerate(skill_names):
        finals.append(max(1, min(12, resolve_skill_level(trio, i) + trace_bonus.get(name, 0))))
    return {
        "charId": char_id,
        "settings": settings,
        "skillLevel": trio,
        "skillNames": skill_names,
        "skillLevelsFinal": finals,
        "traceBonus": trace_bonus,
    }
