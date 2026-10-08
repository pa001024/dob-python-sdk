"""BD 装配：纯 BD JSON（CharSettings + charId）→ 实体装配态。

复刻 createCharBuildFromSettings（CharBuildHelper.ts）与 CharBuild 构造器的
装配语义：实体等级化、BUFF 拆分（普通/code）、武器锻造同步、同律装配、
技能装配（含 MOD 技能替换）、魔灵/潜质 BUFF。计算语义在 engine.py。
"""

from __future__ import annotations

import math
import re as _re

from . import entities
from .entities import level_buff, level_char, level_mod, level_monster, level_skill, level_skill_weapon, level_weapon

# 溯源文本的技能等级加成：[技能名]等级+N（可多个，如 3 溯同时给 E+2、P+1）
TRACE_LEVEL_RE = _re.compile(r"\[([^\]]+)\]等级\+(\d+)")

# 同律槽位在 settings 中的键（对齐 MOD_VARIANT_LEGACY_KEYS）
LEGACY_SLOT_KEYS = {"角色": "charMods", "近战": "meleeMods", "远程": "rangedMods", "同律": "skillWeaponMods"}


def _variant_slots(settings: dict, slot: str) -> list:
    index = settings.get("modVariantIndex") or 0
    if index <= 0:
        return settings.get(LEGACY_SLOT_KEYS[slot]) or []
    variants = settings.get("modVariants") or []
    if 1 <= index <= len(variants):
        return variants[index - 1].get(slot) or settings.get(LEGACY_SLOT_KEYS[slot]) or []
    return settings.get(LEGACY_SLOT_KEYS[slot]) or []


def _variant_aura(settings: dict) -> int:
    index = settings.get("modVariantIndex") or 0
    if index <= 0:
        return settings.get("auraMod") or 0
    variants = settings.get("modVariants") or []
    if 1 <= index <= len(variants):
        aura = (variants[index - 1] or {}).get("中枢")
        if isinstance(aura, (int, float)) and math.isfinite(aura) and aura > 0:
            return int(aura)
    return settings.get("auraMod") or 0


def _mod_buff_lv(effect_config: dict, tables, mod_id: int) -> int:
    key = f"m:{mod_id}"
    if isinstance(effect_config, dict) and effect_config.get(key) is not None:
        return effect_config[key]
    effect = tables.mod_effect_by_id.get(mod_id)
    return (effect or {}).get("mx") or 1


def _weapon_buff_lv(effect_config: dict, tables, weapon_id: int, elm: str = "any") -> int:
    effect = tables.weapon_effect_by_id.get(weapon_id)
    if isinstance(effect, dict) and isinstance(effect.get("限定"), str) and effect["限定"] != elm and elm != "any":
        return 0
    key = f"w:{weapon_id}"
    if isinstance(effect_config, dict) and effect_config.get(key) is not None:
        return effect_config[key]
    return (effect or {}).get("mx") or 1


def _custom_buff(buffs_cfg: list, level: int = 1, coverage: float = 1) -> dict:
    raw: dict = {"名称": "自定义BUFF", "描述": "自行填写"}
    for entry in buffs_cfg or []:
        raw[entry[0]] = entry[1]
    buff = level_buff(raw, level)
    if coverage != 1:
        buff["_coverage"] = coverage
        entities._buff_apply_level(buff, raw)
    return buff


def _buff_from_settings(tables, name: str, level: int, custom_cfg: list, coverage: float = 1) -> dict:
    if name == "自定义BUFF":
        return _custom_buff(custom_cfg, level, coverage)
    raw = tables.buff_by_name.get(name)
    if raw is None:
        raise KeyError(f'Buff "{name}" 未在静态表中找到')
    buff = level_buff(raw, level)
    if coverage != 1:
        buff["_coverage"] = coverage
        entities._buff_apply_level(buff, raw)
    return buff


EMPTY_WEAPON_RAW = {
    "id": 0, "名称": "空武器", "类型": ["近战", "长柄"], "伤害类型": "切割",
    "攻击": 0, "暴击": 0, "暴伤": 0, "触发": 0, "描述": "", "加成": {}, "熔炼": "", "技能": [],
}


def _trait_buffs(tables, slots) -> list:
    out = []
    for slot in slots or []:
        if not isinstance(slot, list) or len(slot) < 2:
            continue
        bid, level = slot[0], slot[1]
        if not isinstance(bid, (int, float)) or not isinstance(level, (int, float)):
            continue
        entry = tables.trait_by_level(int(bid), int(level))
        if not entry:
            continue
        buff_name = f"魔灵潜质:{entry.get('name')}"
        raw = tables.buff_by_name.get(buff_name)
        if raw is None:
            continue
        out.append(level_buff(raw, entry.get("level")))
    return out


def _pet_buffs(tables, settings: dict) -> tuple[list, int]:
    pet_id = settings.get("petId") or 0
    if not pet_id:
        return [], 0
    pet = tables.pet_by_id.get(pet_id)
    if pet is None:
        return [], 0
    traits = settings.get("traits")
    pet_level = settings.get("petLevel")
    base = pet_level if isinstance(pet_level, (int, float)) and math.isfinite(pet_level) else 4
    bonus = 0
    for slot in traits or []:
        if not isinstance(slot, list) or len(slot) < 2:
            continue
        entry = tables.trait_by_level(slot[0], slot[1]) if isinstance(slot[0], (int, float)) else None
        if entry and entry.get("bid") == 1030:
            bonus += 1
    level = max(0, min(4, int(base) + bonus))
    out = []
    passive = tables.buff_by_name.get(pet.get("名称"))
    if passive is not None:
        out.append(level_buff(passive, level))
    active = tables.buff_by_name.get(f"{pet.get('名称')}(主动)")
    if active is not None:
        inst = level_buff(active, level)
        inst["_coverage"] = _resolve_coverage(tables, settings, pet, level)
        entities._buff_apply_level(inst, active)
        out.append(inst)
    base_cd = (pet.get("主动") or {}).get("cd", 0) if isinstance(pet.get("主动"), dict) else 0
    return out, base_cd or 0


def _trait_cd_reduce(tables, slots) -> float:
    reduce = 0.0
    for slot in slots or []:
        if not isinstance(slot, list) or len(slot) < 2:
            continue
        entry = tables.trait_by_level(slot[0], slot[1]) if isinstance(slot[0], (int, float)) else None
        if not entry:
            continue
        buff_name = f"魔灵潜质:{entry.get('name')}"
        raw = tables.buff_by_name.get(buff_name)
        if raw is None:
            continue
        inst = level_buff(raw, entry.get("level"))
        value = inst.get("魔灵CD缩减")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reduce += value
    return max(0.0, min(1.0, reduce))


def _resolve_coverage(tables, settings: dict, pet: dict, level: int) -> float:
    def clamp(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 1.0
        return max(0.0, min(1.0, number if math.isfinite(number) else 1.0))

    manual = settings.get("petCoverage", 1)
    if not settings.get("petAutoCoverage"):
        return clamp(manual)
    active = pet.get("主动") if isinstance(pet.get("主动"), dict) else None
    if not active:
        return clamp(manual)
    desc = active.get("描述") or ""
    import re as _re

    duration_idx = -1
    for i, m in enumerate(_re.finditer(r"\{%?\}", desc)):
        if m.group(0) == "{}" and desc[m.end():].startswith("秒"):
            duration_idx = i
            break
    rows = active.get("值") or []
    duration = 0
    if 0 <= duration_idx < len(rows):
        row = rows[duration_idx] or []
        duration = row[max(0, min(len(row) - 1, level))] if row else 0
    base_cd = active.get("cd") or 0
    cd_reduce = _trait_cd_reduce(tables, settings.get("traits"))
    cooldown = base_cd * (1 - max(0.0, min(1.0, cd_reduce)))
    if not duration or cooldown <= 0:
        return clamp(manual)
    return clamp(duration / cooldown)


def _mod_from_slot(tables, effect_config: dict, entry) -> dict | None:
    if not entry:
        return None
    mod_id, mod_level = entry[0], entry[1]
    raw = tables.mod_by_id.get(mod_id)
    if raw is None:
        raise KeyError(f'MOD ID "{mod_id}" 未在静态表中找到')
    return level_mod(raw, mod_level, _mod_buff_lv(effect_config, tables, mod_id), tables.mod_effect_by_id.get(mod_id))


def _replace_map(mods: list) -> dict:
    out = {}
    for mod in mods:
        if not mod or not mod.get("技能替换"):
            continue
        for skill_id, skill in mod["技能替换"].items():
            out[int(skill_id)] = skill
    return out


def _normalize_weapon_skill(skill_data: dict, fallback: dict) -> dict:
    return {
        "id": skill_data.get("id") or fallback.get("id"),
        "名称": skill_data.get("名称") or fallback.get("名称"),
        "类型": skill_data.get("类型") or fallback.get("类型"),
        "武器": fallback.get("武器"),
        "描述": skill_data.get("描述") or fallback.get("描述"),
        "字段": skill_data.get("字段") or [],
    }


def normalize_skill_levels(value) -> list:
    """复刻 normalizeCharSkillLevels：数字→三元组；数组缺项沿首项补齐；各项取整钳制 1-12。"""
    from .entities import js_round

    def clamp(level, fallback: int) -> int:
        if not isinstance(level, (int, float)) or not math.isfinite(level):
            return fallback
        return max(1, min(12, js_round(level)))

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


def build_state(char_id: int, settings: dict, tables, skill_levels: list | None = None, traces: int | None = None) -> dict:
    """装配一次构筑的全部实体（对齐 createCharBuildFromSettings + CharBuild 构造器）。

    :param skill_levels: 技能等级（数字或 trio 数组；归一化规则同 normalizeCharSkillLevels）
    :param traces: 溯源等级（单数；7 即含 1-7 节点效果），缺省 0（未解锁）
    """
    effect_config = settings.get("effectConfig") or {}
    use_global = settings.get("useGlobal")
    settings = normalize_settings(settings, tables)
    effect_config = settings.get("effectConfig") or {}
    char_raw = tables.char_by_id.get(char_id)
    if char_raw is None:
        raise KeyError(f'角色 "{char_id}" 未在静态表中找到')
    char_level = settings.get("charLevel", 80)
    if char_level is None:
        char_level = 80
    char = level_char(char_raw, char_level)
    # 技能等级 trio：显式参数优先，否则取标准 charSkillLevel（数字/数组均归一化，与 TS 一致）
    raw_levels = skill_levels if skill_levels is not None else settings.get("charSkillLevel", 10)
    trio = normalize_skill_levels(raw_levels)
    trace_texts = list(char_raw.get("溯源") or [])
    trace_level = traces if traces is not None else settings.get("traces", 0)
    trace_bonus = parse_trace_levels(trace_texts, trace_level if trace_level is not None else 0)
    char_skill_raws = char_raw.get("技能") or []
    final_levels = []
    for i in range(len(char_skill_raws)):
        skill_name = (char_skill_raws[i] or {}).get("名称")
        bonus = sum(amount for name, amount in trace_bonus.items() if name == skill_name)
        final_levels.append(max(1, min(12, resolve_skill_level(trio, i) + bonus)))

    def buff_lv(mod_id: int):
        if use_global:
            return None
        return _mod_buff_lv(effect_config, tables, mod_id)

    def wbuff_lv(weapon_id: int):
        if use_global:
            return None
        return _weapon_buff_lv(effect_config, tables, weapon_id, char.get("属性"))

    aura_id = _variant_aura(settings)
    aura = None
    if aura_id:
        aura_raw = tables.mod_by_id.get(aura_id)
        if aura_raw is None:
            raise KeyError(f'MOD ID "{aura_id}" 未在静态表中找到')
        aura = level_mod(aura_raw, None, None, tables.mod_effect_by_id.get(aura_id))
    else:
        aura_raw = None
    char_mods = [_mod_from_slot(tables, effect_config, e) if e else None for e in _variant_slots(settings, "角色")]
    # useGlobal 快照口径（inv）超出纯 BD JSON 范围：统一按本地配置口径处理
    melee_mods = [_mod_from_slot(tables, effect_config, e) if e else None for e in _variant_slots(settings, "近战")]
    ranged_mods = [_mod_from_slot(tables, effect_config, e) if e else None for e in _variant_slots(settings, "远程")]
    skill_mods = [_mod_from_slot(tables, effect_config, e) if e else None for e in _variant_slots(settings, "同律")]
    custom_cfg = settings.get("customBuff") or []
    buffs = []
    for entry in settings.get("buffs") or []:
        coverage = entry[2] if len(entry) > 2 else 1
        try:
            buffs.append(_buff_from_settings(tables, entry[0], entry[1], custom_cfg, coverage))
        except KeyError:
            continue
    buffs.extend(_trait_buffs(tables, settings.get("traits")))
    pet_buff_list, pet_base_cd = _pet_buffs(tables, settings)
    buffs.extend(pet_buff_list)

    def weapon_from(key: str, refine_key: str, level_key: str):
        weapon_id = settings.get(key)
        if weapon_id == 0:
            # 对齐 fromId(0)：零属性空武器占位
            return level_weapon(dict(EMPTY_WEAPON_RAW), settings.get(refine_key), settings.get(level_key), None, None)
        raw = tables.weapon_by_id.get(weapon_id)
        if raw is None:
            raise KeyError(f'武器 "{weapon_id}" 未在静态表中找到')
        return level_weapon(raw, settings.get(refine_key), settings.get(level_key), wbuff_lv(weapon_id), tables.weapon_effect_by_id.get(weapon_id))

    melee_weapon = weapon_from("meleeWeapon", "meleeWeaponRefine", "meleeWeaponLevel")
    ranged_weapon = weapon_from("rangedWeapon", "rangedWeaponRefine", "rangedWeaponLevel")
    # forge 同步：精通不匹配则特效层失效（对齐 syncWeaponForgeEffective）
    for weapon in (melee_weapon, ranged_weapon):
        effective = not weapon.get("_hasForge", False) or _mastered(char, weapon.get("类别", ""), settings.get("extraMastery") or "")
        weapon["forgeEffective"] = effective
        weapon["buffProps"] = weapon["_effectiveBuffProps"] if effective else {}

    # 同律武器装配（对齐 char setter）
    skills = [level_skill(s, final_levels[i] if i < len(final_levels) else trio[2]) for i, s in enumerate(char_skill_raws)]
    skill_weapon = None
    sync_list = char.get("同律武器") or []
    if sync_list:
        uweapon = dict(sync_list[0])
        skill_ids = uweapon.get("skill") or [1]
        sources = [s for i, s in enumerate(skills) if i in skill_ids]
        collected = []
        for source in sources:
            for field in source.get("字段") or []:
                name = field.get("名称") or ""
                filt = uweapon.get("filter") or "伤害"
                import re as _re

                if _re.search(filt, name) and name.endswith("伤害"):
                    collected.append(field)
        uweapon["技能"] = [{"名称": uweapon.get("名称"), "类型": "同律武器伤害", "字段": collected}]
        # 对齐 TS：取 skillIds 首个非负整数下标解析等级（缺省被动），再叠加同律技能名命中的溯源加成
        sync_index = next((i for i in skill_ids if isinstance(i, int) and not isinstance(i, bool) and i >= 0), 2)
        sync_level = max(1, min(12, resolve_skill_level(trio, sync_index) + trace_bonus.get(uweapon.get("名称"), 0)))
        skill_weapon = level_skill_weapon(uweapon, sync_level, char.get("_等级"))
        inherit = skill_weapon.get("inherit")
        if inherit and (melee_weapon if inherit == "melee" else ranged_weapon).get("_isEmpty"):
            pass
        else:
            if inherit:
                target = melee_weapon if inherit == "melee" else ranged_weapon
                skill_weapon["伤害类型"] = target.get("伤害类型")

    state = {
        "char": char,
        "skills": skills,
        "skillWeapon": skill_weapon,
        "charMods": char_mods,
        "meleeMods": melee_mods,
        "rangedMods": ranged_mods,
        "skillMods": skill_mods,
        "auraMod": aura,
        "buffs_all": buffs,
        "buffs": [b for b in buffs if not b.get("code") or len([k for k in b if k not in entities.BUFF_EXCLUDE and not k.startswith("_")]) > 0],
        "dynamicBuffs": [b for b in buffs if b.get("code")],
        "meleeWeapon": melee_weapon,
        "rangedWeapon": ranged_weapon,
        "baseName": settings.get("baseName"),
        "imbalance": bool(settings.get("imbalance")),
        "hpPercent": max(0.0, min(1.0, settings.get("hpPercent", 1) if settings.get("hpPercent") is not None else 1)),
        "resonanceGain": settings.get("resonanceGain", 0) or 0,
        "enemyId": settings.get("enemyId", 130),
        "enemyLevel": settings.get("enemyLevel") or 80,
        "enemyResistance": settings.get("enemyResistance", 0),
        "targetFunction": settings.get("targetFunction") or "伤害",
        "customVariables": [tuple(e) for e in (settings.get("customVariables") or [])],
        "extraMastery": settings.get("extraMastery") or "",
        "skillLevelsFinal": list(final_levels),
        "traceBonus": dict(trace_bonus),
        "teamWeaponCategories": _team_categories(tables, settings),
        "petBaseCd": pet_base_cd,
        "skillLevel": list(trio),
        "settings": settings,
    }
    # 武器技能装配（含 MOD 技能替换）
    state["meleeWeaponSkills"] = _replace_skills(melee_weapon.get("技能") or [], melee_mods)
    state["rangedWeaponSkills"] = _replace_skills(ranged_weapon.get("技能") or [], ranged_mods)
    state["skillWeaponSkills"] = _replace_skills((skill_weapon or {}).get("技能") or [], skill_mods)
    state["weaponSkills"] = state["meleeWeaponSkills"] + state["rangedWeaponSkills"] + state["skillWeaponSkills"]
    state["allSkills"] = state["skills"] + state["weaponSkills"]
    state["skill_name_list"] = [{"名称": s.get("名称"), "safeName": s.get("safeName")} for s in state["allSkills"]]
    e_skill, q_skill, p_skill = (skills + [None, None, None])[:3]
    state["skill_aliases"] = {
        "E": (e_skill or {}).get("safeName"), "Q": (q_skill or {}).get("safeName"), "P": (p_skill or {}).get("safeName"),
    }
    # 敌人
    enemy_raw = tables.monster_by_id.get(state["enemyId"]) or tables.monster_by_id.get(130)
    if enemy_raw is None:
        raise KeyError(f'怪物 "{state["enemyId"]}" 未在静态表中找到')
    state["enemy"] = entities.level_monster(enemy_raw, state["enemyLevel"])
    return state


def _mastered(char: dict, category: str, extra: str) -> bool:
    mastered = char.get("精通") or []
    return category in mastered or "全部类型" in mastered or category == extra


def _round6(value):
    """复刻 roundBuffValue（Number(toFixed(6))）。"""
    try:
        return float(f"{float(value):.6f}")
    except (TypeError, ValueError):
        return value


MOD_SLOT_COUNTS = {"角色": 8, "近战": 8, "远程": 8, "同律": 4}
MOD_SLOT_TYPES = ("角色", "近战", "远程", "同律")


def _normalize_slots(value, length: int) -> list:
    """复刻 normalizeModSlots：固定长度，非法槽位记 null，id/等级取整。"""
    slots = value if isinstance(value, list) else []
    out = []
    for index in range(length):
        slot = slots[index] if index < len(slots) else None
        if not isinstance(slot, list) or len(slot) < 2:
            out.append(None)
            continue
        mod_id, level = slot[0], slot[1]
        if not (isinstance(mod_id, (int, float)) and not isinstance(mod_id, bool) and math.isfinite(mod_id)):
            out.append(None)
            continue
        if not (isinstance(level, (int, float)) and not isinstance(level, bool) and math.isfinite(level)):
            out.append(None)
            continue
        out.append([round(mod_id), round(level)])
    return out


def normalize_trait_slots(slots, tables) -> list:
    """复刻 normalizeTraitSlots：形状校验 + 去重 + 下线档位剔除 + 压紧到 4 槽。"""
    used, picked = set(), []
    for slot in slots or []:
        if not isinstance(slot, list) or len(slot) < 2:
            continue
        bid, level = slot[0], slot[1]
        if not isinstance(bid, int) or isinstance(bid, bool) or not isinstance(level, int) or isinstance(level, bool):
            # TS 仅校验 typeof number；浮点 id 在真实存档不存在，这里同样放行数值型
            if not (isinstance(bid, (int, float)) and isinstance(level, (int, float))):
                continue
            bid, level = int(bid), int(level)
        if bid in used or tables.trait_by_level(bid, level) is None:
            continue
        used.add(bid)
        picked.append([bid, level])
    return (picked + [None] * 4)[:4]


def _migrate_legacy_pet_buffs(settings: dict, tables) -> None:
    """复刻 migrateLegacyPetBuffs：BUFF 列表里的魔灵内容搬进魔灵/潜质字段（原地修改）。"""
    buffs = settings.get("buffs") or []
    if not buffs:
        return
    traits = normalize_trait_slots(settings.get("traits"), tables)
    used = {slot[0] for slot in traits if slot}
    had_pet = bool(settings.get("petId"))
    pet_id = settings.get("petId")
    pet_coverage = settings.get("petCoverage")
    pet_level = settings.get("petLevel")
    pet_level_taken = had_pet
    remaining = []
    migrated = False
    for buff in buffs:
        name = buff[0] if isinstance(buff, list) and buff else None
        level = buff[1] if isinstance(buff, list) and len(buff) > 1 else 0
        coverage = buff[2] if isinstance(buff, list) and len(buff) > 2 else None
        origin = _resolve_pet_buff(name, tables)
        if origin is None:
            remaining.append(buff)
            continue
        migrated = True
        if origin[0] == "trait":
            _, bid = origin
            level0 = max(1, min(3, round(level) if isinstance(level, (int, float)) else 1))
            if bid not in used:
                try:
                    slot_index = traits.index(None)
                except ValueError:
                    slot_index = -1
                if slot_index != -1:
                    traits[slot_index] = [bid, level0]
                    used.add(bid)
            continue
        _, pid, active = origin
        if not pet_id:
            pet_id = pid
        if not had_pet and active and isinstance(coverage, (int, float)):
            pet_coverage = coverage
        if not pet_level_taken and isinstance(level, (int, float)) and math.isfinite(level):
            pet_level = max(0, min(4, round(level)))
            pet_level_taken = True
    if not migrated:
        return
    settings["buffs"] = remaining
    settings["traits"] = traits
    settings["petId"] = pet_id
    settings["petLevel"] = pet_level
    settings["petCoverage"] = pet_coverage


def _resolve_pet_buff(name, tables):
    """复刻 resolvePetBuffName：魔灵潜质名前缀 / 魔灵名 / 魔灵名(主动)。"""
    if not isinstance(name, str):
        return None
    prefix = "魔灵潜质:"
    if name.startswith(prefix):
        trait_name = name[len(prefix):]
        for entry in tables.pet_entries:
            if entry.get("name") == trait_name:
                return ("trait", entry.get("bid"))
        return None
    for pet in tables.pets:
        if pet.get("名称") == name:
            return ("pet", pet.get("id"), False)
        if f"{pet.get('名称')}(主动)" == name:
            return ("pet", pet.get("id"), True)
    return None


def normalize_settings(settings: dict, tables) -> dict:
    """复刻 normalizeCharSettings（读路径子集）：缺键按类型回填默认；特效/覆盖率/潜质/魔灵/变体/技能等级归一化。"""
    defaults = {
        "charLevel": 80, "baseName": "", "hpPercent": 1, "resonanceGain": 3,
        "enemyId": 130, "enemyLevel": 80, "enemyResistance": 0, "isRouge": False,
        "targetFunction": "", "customVariables": [], "charSkillLevel": [10, 10, 10],
        "extraMastery": "", "meleeWeapon": 10206, "meleeWeaponLevel": 80, "meleeWeaponRefine": 5,
        "rangedWeapon": 20102, "rangedWeaponLevel": 80, "rangedWeaponRefine": 5,
        "auraMod": 31524, "imbalance": False,
        "charMods": [None] * 8, "meleeMods": [None] * 8, "rangedMods": [None] * 8,
        "skillWeaponMods": [None] * 4, "modVariantIndex": 0, "modVariants": [],
        "buffs": [], "customBuff": [], "petId": 0, "petLevel": 3, "petCoverage": 1,
        "petAutoCoverage": True, "traits": [None] * 4,
        "team1Weapon": "-", "team2Weapon": "-",
        "useGlobal": False, "effectConfig": {},
        "dotSettings": {"skill": 0, "melee": 0, "ranged": 0, "skillweapon": 0, "forceOwnAdditionalDamage": False},
    }
    normalized = dict(defaults)
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key not in normalized or value is None:
                continue
            default = normalized[key]
            if isinstance(default, list):
                if isinstance(value, list):
                    normalized[key] = value
                continue
            if isinstance(default, dict):
                if isinstance(value, dict):
                    normalized[key] = {**default, **value}
                continue
            if _same_type(value, default):
                normalized[key] = value
    normalized["buffs"] = [list(b) for b in (normalized["buffs"] or [])]
    normalized["customBuff"] = [[p, _round6(v)] for p, v in (normalized["customBuff"] or [])]
    normalized["traits"] = normalize_trait_slots(normalized["traits"], tables)
    _migrate_legacy_pet_buffs(normalized, tables)
    pet_id = normalized.get("petId")
    if not isinstance(pet_id, (int, float)) or isinstance(pet_id, bool) or not math.isfinite(pet_id) or tables.pet_by_id.get(int(pet_id)) is None:
        normalized["petId"] = 0
    else:
        normalized["petId"] = int(pet_id)
    pet_level = normalized.get("petLevel")
    normalized["petLevel"] = max(0, min(4, round(pet_level))) if isinstance(pet_level, (int, float)) and not isinstance(pet_level, bool) and math.isfinite(pet_level) else 4
    pet_coverage = normalized.get("petCoverage")
    normalized["petCoverage"] = max(0.0, min(1.0, _round6(pet_coverage))) if isinstance(pet_coverage, (int, float)) and not isinstance(pet_coverage, bool) and math.isfinite(pet_coverage) else 1
    normalized["petAutoCoverage"] = normalized.get("petAutoCoverage") is not False
    for key in ("team1Weapon", "team2Weapon"):
        value = normalized.get(key)
        if isinstance(value, str) and value != "-":
            found = tables.weapon_by_name.get(value)
            normalized[key] = found["id"] if found else value
    normalized["charSkillLevel"] = normalize_skill_levels(settings.get("charSkillLevel") if isinstance(settings, dict) else None)
    variants = normalized.get("modVariants")
    if not isinstance(variants, list):
        variants = []
    cleaned = []
    for raw in variants[:2]:
        entry = {"中枢": 0}
        for slot in MOD_SLOT_TYPES:
            entry[slot] = _normalize_slots((raw or {}).get(slot) if isinstance(raw, dict) else None, MOD_SLOT_COUNTS[slot])
        aura = (raw or {}).get("中枢") if isinstance(raw, dict) else None
        entry["中枢"] = round(aura) if isinstance(aura, (int, float)) and not isinstance(aura, bool) and math.isfinite(aura) and aura > 0 else 0
        cleaned.append(entry)
    normalized["modVariants"] = cleaned
    index = normalized.get("modVariantIndex")
    normalized["modVariantIndex"] = max(0, min(len(cleaned), round(index))) if isinstance(index, (int, float)) and not isinstance(index, bool) and math.isfinite(index) else 0
    return normalized


def _same_type(value, default) -> bool:
    """typeof 对等判定：bool 独立成类；int/float 互通（对齐 typeof number）。"""
    if isinstance(default, bool) or isinstance(value, bool):
        return type(value) is type(default)
    if isinstance(default, (int, float)) and isinstance(value, (int, float)):
        return True
    return type(value) is type(default)


def _team_categories(tables, settings: dict) -> list:
    out = []
    for key in ("team1Weapon", "team2Weapon"):
        weapon_id = settings.get(key)
        if isinstance(weapon_id, bool):
            continue
        if weapon_id == 0:
            # 对齐 getCategory(0)：空武器占位类别为长柄
            out.append("长柄")
            continue
        if isinstance(weapon_id, (int, float)):
            raw = tables.weapon_by_id.get(int(weapon_id))
            if raw and len(raw.get("类型") or []) > 1:
                out.append(raw["类型"][1])
    return out


def _replace_skills(skills: list, mods: list) -> list:
    replace = _replace_map([m for m in mods if m])
    if not replace:
        return skills
    out = []
    for skill in skills:
        target = replace.get(skill.get("id"))
        if not target:
            out.append(skill)
            continue
        raw = _normalize_weapon_skill(target, skill)
        out.append(level_skill(raw, skill.get("_level"), skill.get("武器名")))
    return out
