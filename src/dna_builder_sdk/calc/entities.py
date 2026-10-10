"""等级化实体：Leveled* 构造器的纯函数复刻（实例 = 普通 dict）。

TS 出处：src/data/leveled/Leveled{Char,Skill,SkillWeapon,Mod,Buff,Weapon,Monster}.ts。
数值缩放、等级钳制、效果层归一化逐项对齐；展示专用（url/描述模板）一律跳过。
"""

from __future__ import annotations

import math

from .curves import COMMON_LEVEL_UP, MOB_LEVEL_UP

MOD_QUALITY_MAX_LEVEL = {"金": 10, "紫": 5, "蓝": 5, "绿": 3, "白": 3}

MOD_EXCLUDE = frozenset(
    [
        "id", "系列", "品质", "耐受", "类型", "名称", "描述", "限定", "极性", "属性",
        "消耗", "技能替换", "_等级", "_originalModData", "buff", "buffLv", "maxLevel",
        "生效", "效果", "code", "count", "icon", "版本", "buffProps", "_effectAppliedKeys",
    ]
)

BUFF_EXCLUDE = frozenset(
    [
        "id", "名称", "描述", "限定", "品质", "_等级", "_originalBuffData", "a", "b",
        "lx", "bx", "mx", "dx", "pid", "pt", "code", "attr", "技能", "_ratio", "_coverage",
    ]
)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def js_round(x: float) -> int:
    """复刻 Math.round（half-up；Python round 为 banker's）。"""
    return math.floor(float(x) + 0.5)


def js_round_n(x: float, n: int) -> float:
    factor = 10**n
    return math.floor(float(x) * factor + 0.5) / factor


# ---------- BUFF ----------

def _buff_prop_name(prop: str) -> str:
    return prop[1:] if prop.startswith("@") else prop


def level_buff(raw: dict, level: int | None = None, coverage: float = 1.0, ratio: float = 1.0) -> dict:
    """复刻 LeveledBuff 构造 + updatePropertiesByLevel。"""
    inst: dict = {
        "名称": raw.get("名称"),
        "描述": raw.get("描述", ""),
        "_original": raw,
        "_coverage": coverage,
        "_ratio": ratio,
    }
    for key in ("a", "b", "lx", "mx", "dx", "技能", "code", "attr"):
        if raw.get(key) is not None and not (key in ("a", "b") and raw.get(key) == 1):
            if key in ("a", "b") and raw.get(key) == 1:
                continue
            inst[key] = raw[key]
    if raw.get("mx") is not None:
        inst["mx"] = raw["mx"]
        inst["dx"] = raw.get("dx", raw["mx"])
    inst["_等级"] = level if level is not None and level >= 0 else (inst.get("dx") or inst.get("mx") or 1)
    _buff_apply_level(inst, raw)
    return inst


def _buff_apply_level(inst: dict, raw: dict) -> None:
    level = inst["_等级"]
    if raw.get("lx") is not None:
        level = max(raw["lx"], level)
    level = min(raw.get("mx") or 1, level)
    inst["_等级"] = level
    a = inst.get("a") or 1
    b = inst.get("b") or 1
    lx = raw.get("lx") if raw.get("lx") is not None else 1
    for prop in raw:
        if prop in BUFF_EXCLUDE:
            continue
        max_value = raw[prop]
        if max_value is None:
            continue
        name = _buff_prop_name(prop)
        base = raw.get("lx") if raw.get("lx") is not None else 1
        if isinstance(max_value, list):
            inst[name] = max_value[min(level, len(max_value)) - base] * inst["_ratio"] * inst["_coverage"]
        elif _is_num(max_value):
            value = (max_value / a) * (1 + (level - lx) / b) * inst["_ratio"] * inst["_coverage"]
            if name == "神智回复":
                value = js_round(value)
            inst[name] = value


def buff_properties(inst: dict) -> list[str]:
    return [k for k in inst if k not in BUFF_EXCLUDE and not k.startswith("_")]


def buff_is_layer_prop(raw: dict, prop: str) -> bool:
    return f"@{prop}" in raw


# ---------- MOD ----------

def level_mod(raw: dict, level: int | None = None, buff_lv: int | None = None, effect_raw: dict | None = None) -> dict:
    """复刻 LeveledMod 构造 + updateProperties（含特效层归一化）。"""
    max_level = MOD_QUALITY_MAX_LEVEL.get(raw.get("品质"), 1)
    inst: dict = {
        "id": raw.get("id"),
        "名称": raw.get("名称"),
        "系列": raw.get("系列"),
        "品质": raw.get("品质"),
        "耐受": raw.get("耐受"),
        "类型": raw.get("类型"),
        "_original": raw,
        "maxLevel": max_level,
        "buffProps": {},
        "_effectKeys": [],
    }
    for key in ("极性", "属性", "限定", "效果", "消耗", "技能替换"):
        if raw.get(key) is not None:
            inst[key] = raw[key]
    buff_inst = None
    if effect_raw is not None and (not effect_raw.get("品质") or effect_raw.get("品质") == inst["品质"]):
        buff_inst = level_buff(effect_raw, buff_lv)
        inst["buff"] = buff_inst
    inst["_等级"] = max(0, min(max_level, level)) if level is not None else max_level
    _mod_apply_level(inst, raw)
    return inst


def _mod_apply_level(inst: dict, raw: dict) -> None:
    max_level = inst["maxLevel"]
    level = inst["_等级"]
    mod_id = inst["id"]
    if mod_id is not None and mod_id > 100000:
        inst["耐受"] = raw.get("耐受", 0) + max_level - level
    else:
        inst["耐受"] = raw.get("耐受", 0) - max_level + level
    if raw.get("技能替换") and mod_id is not None and mod_id > 200000:
        ratio = 1 + ((level * 10 + 100) - 100) / 200
        inst["技能替换"] = _scale_replace(raw["技能替换"], ratio)
    elif raw.get("技能替换"):
        inst["技能替换"] = raw["技能替换"]
    if raw.get("生效"):
        scaled: dict = {"条件": raw["生效"].get("条件")}
        for key, mv in raw["生效"].items():
            if key == "条件":
                continue
            if key in inst:
                del inst[key]
            if isinstance(mv, list):
                scaled[key] = [(mv[0] / (max_level + 1)) * (level + 1), (mv[1] / (max_level + 1)) * (level + 1)]
            else:
                value = (mv / (max_level + 1)) * (level + 1)
                if key in ("神智回复", "最大耐受"):
                    value = math.ceil(value)
                scaled[key] = value
        inst["生效"] = scaled
    for prop in raw:
        if prop in MOD_EXCLUDE:
            continue
        max_value = raw[prop]
        if not max_value:
            continue
        lv = level
        if inst.get("系列") in ("换生灵", "海妖") and prop == "减伤":
            lv = max_level
        if (mod_id is not None and 100000 < mod_id < 150000) or (mod_id is not None and 200000 < mod_id < 250000):
            lv = max_level
        if isinstance(max_value, list):
            inst[prop] = max_value[min(lv + 1, len(max_value)) - 1]
        else:
            value = (max_value / (max_level + 1)) * (lv + 1)
            if prop in ("神智回复", "最大耐受"):
                value = math.ceil(value)
            inst[prop] = value
    buff_props: dict = {}
    buff = inst.get("buff")
    if buff is not None:
        for prop in buff_properties(buff):
            max_value = _num_or_zero(buff.get(prop))
            value = (max_value / (max_level + 1)) * (level + 1)
            if prop in ("神智回复", "最大耐受"):
                value = math.ceil(value)
            if buff_is_layer_prop(buff["_original"], prop):
                buff_props[prop] = buff_props.get(prop, 0) + value
            else:
                target = _resolve_effect_prop(inst, prop)
                if target is None:
                    continue
                inst[target] = (inst[target] if _is_num(inst.get(target)) else 0) + value
                inst["_effectKeys"].append(target)
    inst["buffProps"] = buff_props


def _scale_replace(replace: dict, ratio: float) -> dict:
    if ratio == 1:
        return replace
    if ratio == 1:
        return replace
    out = {}
    for skill_id, skill in replace.items():
        entry = dict(skill)
        fields = []
        for field in entry.get("字段") or []:
            item = dict(field)
            # 字段数值按比例缩放（对齐 scaleSkillReplaceByRatio 的数值键处理）
            for key in ("值", "值2"):
                if _is_num(item.get(key)):
                    item[key] = item[key] * ratio
            fields.append(item)
        entry["字段"] = fields
        out[skill_id] = entry
    return out


def _resolve_effect_prop(inst: dict, prop: str) -> str | None:
    """复刻 resolveEffectProperty：类型前缀剥离；纯槽位名返回 None。"""
    mod_type = inst.get("类型") or ""
    if mod_type and prop.startswith(mod_type):
        return prop[len(mod_type):]
    if prop in ("近战", "远程") or prop.startswith("同律"):
        return None
    return prop


def mod_properties(inst: dict) -> list[str]:
    return [k for k in inst if k not in MOD_EXCLUDE and not k.startswith("_")]


def mod_add_attr(inst: dict) -> dict:
    return {prop: inst[prop] for prop in mod_properties(inst) if _is_num(inst.get(prop))}


def _num_or_zero(value) -> float:
    return float(value) if _is_num(value) else 0.0


# ---------- SKILL ----------

def level_skill_fields(raw_fields, level: int) -> list[dict]:
    """复刻 LeveledSkill.updateProperties：数组值按{level-1}取档，格式推导基础。"""
    if isinstance(raw_fields, dict):
        items = list(raw_fields.values())
    else:
        items = list(raw_fields or [])
    out = []
    for fo in items:
        if not isinstance(fo, dict):
            continue
        key = fo.get("名称")
        obj = dict(fo)
        obj["safeName"] = (key or "").replace("/", "_")
        value = fo.get("值")
        obj["值"] = value[level - 1] if isinstance(value, list) else value
        if obj.get("值2"):
            raw2 = fo.get("值2")
            obj["值2"] = raw2[level - 1] if isinstance(raw2, list) else raw2
        if obj.get("格式"):
            match = re_match_jp(obj["格式"])
            if match:
                obj["基础"] = match
        out.append(obj)
    return out


def re_match_jp(fmt: str) -> str | None:
    import re as _re

    match = _re.search("生命|防御", fmt or "")
    return match.group(0) if match else None


def level_skill(raw: dict, level: int | None = None, weapon_name: str | None = None) -> dict:
    level = max(1, min(12, level or 10))
    inst = {
        "id": raw.get("id") or 0,
        "名称": raw.get("名称") or f"SKILL{raw.get('id')}",
        "safeName": (raw.get("名称") or f"SKILL{raw.get('id')}").replace("/", "_"),
        "类型": raw.get("类型"),
        "描述": raw.get("描述"),
        "_level": level,
        "_original": raw,
    }
    for key in ("武器", "术语解释", "实体", "召唤物"):
        if raw.get(key) is not None:
            inst[key] = raw[key]
    if weapon_name is not None:
        inst["武器名"] = weapon_name
    inst["子技能"] = [(s.get("名称") or (str(s.get("id")) if s.get("id") else f"子技能{i + 1}")) for i, s in enumerate(raw.get("子技能") or []) if (s.get("名称") or s.get("id"))]
    inst["字段"] = level_skill_fields(raw.get("字段"), level)
    return inst


# ---------- CHAR ----------

def level_char(raw: dict, level: int | None = None) -> dict:
    inst = {
        "id": raw.get("id"),
        "名称": raw.get("名称"),
        "属性": raw.get("属性"),
        "精通": list(raw.get("精通") or []),
        "基础攻击": raw.get("基础攻击"),
        "基础生命": raw.get("基础生命"),
        "基础护盾": raw.get("基础护盾"),
        "基础防御": raw.get("基础防御"),
        "基础神智": raw.get("基础神智"),
        "_original": raw,
        "_等级": max(1, min(80, level if level is not None else 80)),
    }
    for key in ("额外精通", "溯源", "别名", "阵营", "加成", "同律武器", "icon"):
        if raw.get(key) is not None:
            inst[key] = raw[key]
    # 复刻 updatePropertiesByLevel：基础神智不受等级影响
    mult = COMMON_LEVEL_UP[inst["_等级"] - 1]
    inst["基础攻击"] = js_round_n((raw.get("基础攻击") or 0) * mult, 2) if isinstance(raw.get("基础攻击"), (int, float)) else raw.get("基础攻击")
    inst["基础生命"] = js_round((raw.get("基础生命") or 0) * mult)
    inst["基础护盾"] = js_round((raw.get("基础护盾") or 0) * mult)
    return inst


# ---------- WEAPON ----------

def level_weapon(raw: dict, refine: int | None = None, level: int | None = None, effect_lv: int | None = None, effect_raw: dict | None = None) -> dict:
    has_forge = bool(raw.get("熔炉"))
    inst: dict = {
        "id": raw.get("id"),
        "名称": raw.get("名称"),
        "描述": raw.get("描述", ""),
        "_original": raw,
        "buffProps": {},
        "_effectiveBuffProps": {},
        "forgeEffective": True,
        "倍率": 1,
        "弹片数": None,
    }
    weapon_type = raw.get("类型") or []
    inst["类型"] = weapon_type[0] if weapon_type else ""
    inst["类别"] = weapon_type[1] if len(weapon_type) > 1 else ""
    inst["伤害类型"] = raw.get("伤害类型")
    inst["基础攻击"] = raw.get("攻击")
    inst["基础暴击"] = raw.get("暴击")
    inst["基础暴伤"] = raw.get("暴伤")
    inst["基础触发"] = raw.get("触发")
    if raw.get("技能"):
        skills = []
        for v in raw["技能"]:
            skill = {"id": v.get("id"), "名称": v.get("名称"), "武器": inst["类型"], "类型": v.get("类型"), "描述": v.get("描述")}
            if v.get("字段") is not None:
                skill["字段"] = v["字段"]
            if v.get("实体") is not None:
                skill["实体"] = v["实体"]
            skills.append(skill)
        inst["技能"] = [level_skill(s, None, raw.get("名称")) for s in skills]
        inst["弹道类型"] = "非弹道" if any(f and f.get("名称") == "射线伤害" for v in raw["技能"] for f in (v.get("字段") or [])) else "弹道"
    buff_inst = None
    if effect_raw is not None:
        buff_inst = level_buff(effect_raw, effect_lv)
        inst["buff"] = buff_inst
    interval = raw.get("射击间隔")
    inst["射速"] = js_round_n(1 / interval, 4) if interval else None
    inst["基础装填"] = raw.get("装填") or 0
    inst["基础弹匣"] = raw.get("弹匣")
    inst["基础弹药"] = raw.get("最大弹药")
    inst["_精炼"] = 0 if has_forge else max(0, min(5, refine if refine is not None else 5))
    inst["_等级"] = max(1, min(80, level if level is not None else 80))
    inst["_hasForge"] = has_forge
    inst["_isEmpty"] = inst["id"] == 0
    inst["_isSkillWeapon"] = False
    _weapon_apply(inst, raw, has_forge)
    return inst


def _weapon_apply(inst: dict, raw: dict, has_forge: bool) -> None:
    level = max(1, min(80, inst["_等级"]))
    inst["基础攻击"] = js_round_n(raw.get("攻击", 0) * COMMON_LEVEL_UP[level - 1], 2)
    refine_level = 5 if has_forge else inst["_精炼"]
    scale = not has_forge
    additions = raw.get("加成") or {}
    for prop, original in additions.items():
        if original is None:
            continue
        inst[prop] = (original / 5) * (refine_level + 5) if scale else original
    inst["效果"] = raw.get("熔炼")
    buff = inst.get("buff")
    if buff is not None:
        ratio = (refine_level + 5) / 10
        buff["_ratio"] = ratio
        _buff_apply_level(buff, buff["_original"])
        props = {p: buff[p] for p in buff_properties(buff) if _is_num(buff.get(p))}
        inst["_effectiveBuffProps"] = props
        inst["buffProps"] = props if inst.get("forgeEffective", True) else {}


def level_skill_weapon(raw: dict, skill_level: int | None = None, level: int | None = None, char_skills: list | None = None) -> dict:
    """复刻 LeveledSkillWeapon 构造（含同律技能字段装配由调用方完成）。"""
    weapon_type = raw.get("类型") or []
    inst: dict = {
        "id": raw.get("id"),
        "名称": raw.get("名称"),
        "类型": (weapon_type[0] if len(weapon_type) > 0 else "") + (weapon_type[1] if len(weapon_type) > 1 else ""),
        "类别": weapon_type[2] if len(weapon_type) > 2 else "",
        "伤害类型": raw.get("伤害类型") or "切割",
        "基础攻击": raw.get("攻击") or 0,
        "基础暴击": raw.get("暴击") or 0,
        "基础暴伤": raw.get("暴伤") or 0,
        "基础触发": raw.get("触发") or 0,
        "倍率": 1,
        "弹片数": None,
        "射速": raw.get("攻速") or 1,
        "基础装填": 0,
        "_original": raw,
        "_技能等级": max(1, min(12, skill_level if skill_level is not None else 10)),
        "_等级": max(1, min(80, level if level is not None else 80)),
        "_isSkillWeapon": True,
        "_isEmpty": False,
    }
    for key in ("inherit", "atk", "视为"):
        if raw.get(key) is not None:
            inst[key] = raw[key]
    if raw.get("技能"):
        inst["技能"] = [level_skill({**s, "武器": inst["类型"]}, inst["_技能等级"], raw.get("名称")) for s in raw["技能"]]
    # 复刻 updateProperties：基础攻击按武器等级吃曲线（暴击/暴伤/触发不缩放）
    inst["基础攻击"] = (raw.get("攻击") or 0) * COMMON_LEVEL_UP[max(1, min(80, inst["_等级"])) - 1]
    return inst


# ---------- MONSTER ----------

def level_monster(raw: dict, level: int = 80, is_rouge: bool = False) -> dict:
    mult = MOB_LEVEL_UP[max(1, min(240, level)) - 1]
    inst = {
        "id": raw.get("id"),
        "n": raw.get("n"),
        "f": raw.get("f") or 0,
        "atk": js_round(raw.get("atk", 0) * mult["atk"]),
        "def": raw.get("def", 0),
        "hp": js_round(raw.get("hp", 0) * (mult["rhp"] if is_rouge else mult["hp"])),
        "_等级": max(1, min(240, level)),
        "_original": raw,
    }
    for key in ("t", "es", "tn", "icon", "tags"):
        if raw.get(key) is not None:
            inst[key] = raw[key]
    if raw.get("es") is not None:
        inst["es"] = js_round(raw["es"] * (mult["res"] if is_rouge else mult["es"]))
    inst["currentHP"] = inst["hp"]
    inst["currentShield"] = inst.get("es") or 0
    inst["currentTN"] = inst.get("tn") or 0
    return inst


def _summon_fields(summon: dict, skill: dict, attrs: dict) -> list[dict]:
    """复刻 LeveledSkill.getSummonAttrs（召唤物附加字段；无召唤物时为空）。"""
    import math as _math

    atkspd = attrs.get("召唤物攻击速度") or 0
    duration_field = next((f for f in skill.get("字段") or [] if isinstance(f, dict) and "召唤物" in (f.get("名称") or "") and "持续时间" in (f.get("名称") or "")), None)
    duration = 0
    if duration_field:
        raw = duration_field.get("值") or 0
        impact = duration_field.get("影响") or ""
        duration = raw * (attrs.get("技能耐久") or 1) if "技能耐久" in impact else raw
    interval = (summon.get("攻击间隔") or 1) / (1 + atkspd) if (1 + atkspd) != 0 else 0
    delay = summon.get("攻击延迟") or 0
    attack_times = _math.floor((duration - delay) / interval) if interval else 0
    scope_range = min(2.8, (attrs.get("技能范围") or 1) * (1 + (attrs.get("召唤物范围") or 0)))
    rows = [
        {"名称": "召唤物名称", "格式": summon.get("名称"), "值": 0},
        {"名称": "召唤物攻击延迟", "值": delay, "格式": "{}秒"},
        {"名称": "召唤物攻击间隔", "值": interval, "格式": "{}秒"},
        {"名称": "召唤物攻速", "值": atkspd},
        {"名称": "召唤物攻击次数", "值": attack_times, "格式": "{}"},
        {"名称": "召唤物范围", "值": scope_range},
    ]
    for row in rows:
        row["safeName"] = row["名称"].replace("/", "_")
    return rows


def mod_check_condition(mod: dict, attrs: dict, char_mods: list, cond_values: dict | None = None):
    """复刻 LeveledMod.checkCondition。"""
    eff = mod.get("生效") or {}
    conditions = eff.get("条件")
    if not conditions:
        return None
    polar: dict = {}
    id_counts: dict = {}
    for m in char_mods:
        if not m:
            continue
        if m.get("极性"):
            polar[m["极性"]] = polar.get(m["极性"], 0) + 1
        id_counts[m.get("id")] = id_counts.get(m.get("id"), 0) + 1
    max_count = max([0] + list(id_counts.values()))

    def check(actual, op, value) -> bool:
        if op == "*":
            return True
        if op == "=":
            return actual == value
        if op == ">":
            return actual > value
        if op == ">=":
            return actual >= value
        if op == "<":
            return actual < value
        if op == "<=":
            return actual <= value
        return False

    for attr, op, value in conditions:
        if attr == "*id":
            actual = max_count
        elif attr[:1] in polar:
            actual = polar[attr[:1]]
        elif cond_values is not None and attr in cond_values:
            actual = cond_values[attr]
        else:
            raw = attrs.get(attr, 0)
            actual = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
        if not check(actual, op, value):
            return {"isEffective": False, "props": {k: v for k, v in eff.items() if k != "条件"}}
    return {"isEffective": True, "props": {k: v for k, v in eff.items() if k != "条件"}}


def mod_apply_condition(mod: dict, attrs: dict, char_mods: list, cond_values: dict | None = None) -> bool:
    """复刻 LeveledMod.applyCondition：改写 MOD 自身属性（非 attrs）。"""
    condition = mod_check_condition(mod, attrs, char_mods, cond_values)
    if not condition:
        return False
    changed = False
    for key, prop in condition["props"].items():
        if condition["isEffective"]:
            if isinstance(prop, list):
                cond = (mod.get("生效") or {}).get("条件", [[None]])[0][0]
                base = (cond_values or {}).get(cond, attrs.get(cond, 0))
                try:
                    base_num = float(base)
                except (TypeError, ValueError):
                    base_num = 0.0
                final = min(base_num * prop[0], prop[1])
            else:
                final = prop
            if mod.get(key) != final:
                mod[key] = final
                changed = True
        elif mod.get(key):
            del mod[key]
            changed = True
    return changed


def monster_hp_type(monster: dict) -> str:
    return "护盾" if (monster.get("currentShield") or 0) > 0 else "生命"


def _merge_conditional(base: dict, props: dict) -> dict:
    """复刻 LeveledSkill.mergeConditionalProps（乘法池 Π-1，其余加算）。"""
    from .damage import _is_mult  # 延迟导入避免循环

    merged = dict(base)
    for prop, value in props.items():
        if _is_mult(prop):
            merged[prop] = (1 + (merged.get(prop) or 0)) * (1 + value) - 1
        else:
            merged[prop] = (merged.get(prop) or 0) + value
    return merged


def _resolve_ranged_multi(attrs: dict | None, ranged_multi=None) -> float:
    """复刻 LeveledSkill 影响:多重 的取值：显式参数 > rangedWeapon.多重 > weapon.多重 > 1。"""
    if ranged_multi is not None:
        return ranged_multi
    for key in ("rangedWeapon", "weapon"):
        panel = (attrs or {}).get(key)
        if isinstance(panel, dict):
            value = panel.get("多重")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return 1


def level_skill_fields_with_attr(skill: dict, attrs: dict | None, rules: list, ranged_multi=None) -> list[dict]:
    """复刻 LeveledSkill.getFieldsWithAttr：条件合并 → 技能倍率赋值 → 影响缩放。

    :param skill: level_skill 产物（含 字段）
    :param attrs: 当前角色属性（None 时原样返回字段）
    :param rules: [(compiled_pattern, props)] 条件列表
    :param ranged_multi: 远程武器多重射击（1 开始），显式传入时优先，否则按 attrs 回退读取
    """
    tt = {
        "技能威力": (attrs or {}).get("技能威力") or 1,
        "技能耐久": (attrs or {}).get("技能耐久") or 1,
        "技能效益": (attrs or {}).get("技能效益") or 1,
        "技能范围": (attrs or {}).get("技能范围") or 1,
        "多重": _resolve_ranged_multi(attrs, ranged_multi),
    }
    out = []
    for field in skill.get("字段") or []:
        if not isinstance(field, dict):
            continue
        field_attrs = attrs
        if attrs and rules:
            matched = [props for pattern, props in rules if pattern.search(field.get("名称") or "")]
            for props in matched:
                field_attrs = _merge_conditional(field_attrs, props)
        if field_attrs and field_attrs.get("技能倍率赋值") and "伤害" in (field.get("名称") or ""):
            out.append({
                **field,
                "值": field_attrs["技能倍率赋值"] * (1 + (field_attrs.get("技能倍率乘数") or 0)) + field_attrs.get("技能倍率加数", 0),
            })
            continue
        if field.get("影响"):
            val = field.get("值")
            val2 = field.get("值2") or 0
            if "伤害" in (field.get("名称") or ""):
                val = val * (1 + (field_attrs.get("技能倍率乘数") or 0)) + (field_attrs.get("技能倍率加数") or 0) if field_attrs else val
            props = set(str(field["影响"]).split(","))
            if "技能范围" in props:
                val = val * tt["技能范围"]
            if "技能威力" in props:
                val = val * tt["技能威力"]
                val2 = val2 * tt["技能威力"]
            if "技能耐久" in props:
                if "每秒神智消耗" in (field.get("名称") or ""):
                    val = val / tt["技能耐久"]
                else:
                    val = val * tt["技能耐久"]
            if "技能效益" in props:
                if "技能耐久" in props:
                    val = field.get("值") * max(0.25, (2 - tt["技能效益"]) / tt["技能耐久"])
                else:
                    val = field.get("值") * (2 - tt["技能效益"])
            if "多重" in props:
                # 多重影响：读取远程武器多重射击属性（1 开始，无远程武器时为 1，即无加成）
                val = val * tt["多重"]
                val2 = val2 * tt["多重"]
            if "神智消耗" in (field.get("名称") or ""):
                import math as _math

                val = _math.ceil(val)
            out.append({**field, "值": val, "值2": val2})
            continue
        out.append(field)
    summon = skill.get("召唤物")
    if summon and attrs is not None:
        out.extend(_summon_fields(summon, skill, attrs))
    return out
