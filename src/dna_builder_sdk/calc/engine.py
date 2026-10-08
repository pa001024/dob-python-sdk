"""构筑聚合引擎：纯 BD JSON + 原始表 → 属性/面板/技能表/战斗状态 → 目标值。

复刻 src/data/CharBuild.ts 的只读计算链：
建表（buildBonusSourceTable）→ getTotalBonus/Mul/Reduce → 40 维向量 →
calculateAttributes（含 MOD 条件 / attr-BUFF / code-BUFF）→
calculateWeaponAttributes（含充盈/召唤物转化）→ 面板/技能表/战斗 → DamageContext →
evaluate 目标函数。不含编辑语义（无 AutoBuild/变配装），无缓存（每次重算）。
"""

from __future__ import annotations

import math
import re

from . import entities
from .ast import parse_ast
from .damage import DamageContext
from .entities import js_round, js_round_n, level_buff
from .evaluate import evaluate
from .jscode import run_js_code

CHARACTER_BONUS = [
    "攻击", "固定攻击", "固定生命", "生命", "护盾", "防御", "神智", "属性攻击",
    "技能威力", "技能耐久", "技能效益", "技能范围", "昂扬", "背水", "增伤",
    "元素增伤", "物理增伤", "武器伤害", "技能伤害", "技能速度", "属性穿透",
    "失衡易伤", "技能倍率加数", "召唤物属性继承比例", "召唤物攻击速度", "召唤物范围",
    "召唤物伤害", "召唤物独立增伤", "技能倍率赋值", "转切割", "转贯穿", "转震荡",
    "转灾厄", "转充盈", "转属克", "转属逆", "充盈威力", "技能触发", "异常数量", "魔灵CD缩减",
]

CHAR_MACROS = {
    "ATK": "攻击", "DEF": "防御", "HP": "生命", "SP": "神智",
    "DPH": "or(多重,1)*伤害", "总伤": "max(1,召唤物攻击次数)*伤害",
    "暴击伤害": "伤害.暴击", "DPS": "or(攻速,1+技能速度)*or(多重,1)*伤害",
    "范围收益": "技能范围*伤害", "耐久收益": "技能耐久*伤害", "效益收益": "技能效益*伤害",
    "每神智DPH": "1/神智消耗*伤害", "每持续神智DPH": "1/每秒神智消耗*伤害",
    "每神智DPS": "or(攻速,1+技能速度)/神智消耗*伤害",
    "每持续神智DPS": "or(攻速,1+技能速度)/每秒神智消耗*伤害",
}

ATTACK_TYPE_TAGS = [
    ("普攻", ("普攻", "普通攻击")),
    ("蓄力", ("蓄力攻击",)),
    ("下落", ("下落攻击",)),
    ("滑行", ("滑行攻击",)),
]

HP_TYPE_COEFFICIENTS = {"生命": 0.5, "护盾": 1, "战姿": 1}
HP_TYPE_DMG = {"生命": "贯穿", "护盾": "切割", "战姿": "震荡"}

ELM_SERIES = ("狮鹫", "百首", "契约者", "换生灵")  # 对齐 CharBuild.elmSeries


def _num(value) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


class ZeroDict(dict):
    """code 沙箱槽位记录：缺键读 0（对齐 createZeroFilledRecord）。"""

    def __missing__(self, key):
        return 0


class Engine:
    """一次构筑的聚合状态机（state 为 build.py 装配的实体 dict）。"""

    def __init__(self, state: dict, tables):
        self.s = state
        self.t = tables
        self._table = None
        self._mods_mul: dict = {}
        self._buffs_mul: dict = {}

    # ---------- MOD 列表 ----------
    def mods(self) -> list:
        out = []
        for slot in (self.s["charMods"], self.s["meleeMods"], self.s["rangedMods"], self.s["skillMods"]):
            out.extend(m for m in slot if m)
        if self.s.get("auraMod"):
            out.append(self.s["auraMod"])
        return out

    def scoped_mods(self, scope: str) -> list:
        if not scope:
            return self.mods()
        return [m for m in self.mods() if m.get("类型") == scope]

    # ---------- 作用域 ----------
    @staticmethod
    def scope_of(prefix: str = "角色") -> str:
        if prefix.startswith("同律近战"):
            return "同律近战"
        if prefix.startswith("同律远程"):
            return "同律远程"
        if prefix.startswith("近战"):
            return "近战"
        if prefix.startswith("远程"):
            return "远程"
        return prefix

    @staticmethod
    def buff_in_scope(attribute: str, scope: str) -> bool:
        attr_scope = Engine.scope_of(attribute)
        if attr_scope == "近战" or attr_scope == "远程" or attr_scope.startswith("同律"):
            return scope == attr_scope
        return True

    # ---------- 汇总表 ----------
    def bonus_table(self) -> dict:
        if self._table is not None:
            return self._table
        table = {
            "char": {}, "melee": {}, "ranged": {},
            "modsChar": {}, "modsByScope": {}, "modsAll": {},
            "modsBuffProps": {}, "buffs": {}, "weaponBuffProps": {},
        }

        def acc(target: dict, source: dict | None, keys=None):
            if not source:
                return
            for key in keys if keys is not None else source:
                value = source.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    target[key] = target.get(key, 0) + value

        char = self.s["char"]
        acc(table["char"], char.get("加成"))
        melee, ranged = self.s["meleeWeapon"], self.s["rangedWeapon"]
        acc(table["melee"], melee)
        acc(table["ranged"], ranged)
        acc(table["weaponBuffProps"], melee.get("buffProps"))
        acc(table["weaponBuffProps"], ranged.get("buffProps"))
        for mod in self.mods():
            props = entities.mod_properties(mod)
            acc(table["modsAll"], mod, props)
            bucket = table["modsByScope"].setdefault(mod.get("类型"), {})
            acc(bucket, mod, props)
            if mod.get("类型") == "角色":
                acc(table["modsChar"], mod, props)
            acc(table["modsBuffProps"], mod.get("buffProps"))
        for buff in self.s["buffs"]:
            if isinstance(buff.get("技能"), str):
                continue
            acc(table["buffs"], buff)
        self._table = table
        return table

    def sum_mods(self, table: dict, attribute: str, scope: str, include_mods: bool) -> float:
        if not include_mods:
            return 0.0
        if attribute in ("暴击", "暴伤", "触发", "攻速", "充盈转化", "召唤物攻击速度转化", "召唤物范围转化"):
            bonus = table["modsChar"].get(attribute, 0)
            if scope != "角色":
                bonus += table["modsByScope"].get(scope, {}).get(attribute, 0)
            return bonus
        if not scope:
            return table["modsAll"].get(attribute, 0)
        return table["modsByScope"].get(scope, {}).get(attribute, 0)

    def scoped_mods_mul(self, table: dict, attribute: str, scope: str) -> float:
        key = (scope, attribute)
        if key in self._mods_mul:
            return self._mods_mul[key]
        product = 1.0
        for mod in self.scoped_mods(scope):
            value = mod.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                product *= 1 + value
        self._mods_mul[key] = product
        return product

    def buffs_mul(self, table: dict, attribute: str) -> float:
        if attribute in self._buffs_mul:
            return self._buffs_mul[attribute]
        product = 1.0
        for buff in self.s["buffs"]:
            if isinstance(buff.get("技能"), str):
                continue
            value = buff.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                product *= 1 + value
        self._buffs_mul[attribute] = product
        return product

    # ---------- 总加成 ----------
    def mastered(self, category: str) -> bool:
        char = self.s["char"]
        return category in (char.get("精通") or []) or "全部类型" in (char.get("精通") or []) or category == (self.s.get("extraMastery") or "")

    def forge_effective(self, weapon: dict) -> bool:
        return not weapon.get("_hasForge") or self.mastered(weapon.get("类别", ""))

    def get_total(self, attribute: str, prefix: str = "角色", include_mods: bool = True) -> float:
        table = self.bonus_table()
        scope = self.scope_of(prefix)
        bonus = 0.0
        if prefix == "角色" or attribute != "攻击":
            bonus += table["char"].get(attribute, 0)
        melee, ranged = self.s["meleeWeapon"], self.s["rangedWeapon"]
        if scope == "角色" or (scope == "近战" and attribute != "攻击"):
            if self.forge_effective(melee):
                bonus += table["melee"].get(attribute, 0)
        if scope == "角色" or (scope == "远程" and attribute != "攻击"):
            if self.forge_effective(ranged):
                bonus += table["ranged"].get(attribute, 0)
        bonus += self.sum_mods(table, attribute, scope, include_mods)
        in_scope = self.buff_in_scope(attribute, scope)
        shared = scope == "角色" or (attribute != "攻击" and attribute != "增伤")
        if in_scope and shared:
            bonus += table["buffs"].get(attribute, 0)
        if in_scope and shared:
            bonus += table["modsBuffProps"].get(attribute, 0)
        if shared:
            bonus += table["weaponBuffProps"].get(attribute, 0)
        return bonus

    def get_total_mul(self, attribute: str, prefix: str = "角色", include_mods: bool = True) -> float:
        table = self.bonus_table()
        scope = self.scope_of(prefix)
        bonus = 1.0
        char_value = (self.s["char"].get("加成") or {}).get(attribute)
        if isinstance(char_value, (int, float)) and not isinstance(char_value, bool):
            bonus *= 1 + char_value or 0
        if include_mods:
            bonus *= self.scoped_mods_mul(table, attribute, scope)
        in_scope = self.buff_in_scope(attribute, scope)
        shared = scope == "角色" or attribute != "独立增伤"
        if in_scope and shared:
            bonus *= self.buffs_mul(table, attribute)
        return bonus - 1

    def get_total_reduce(self, attribute: str, prefix: str | None = None) -> float:
        bonus = 0.0
        for mod in self.mods():
            if prefix and mod.get("类型") != prefix:
                continue
            value = mod.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bonus = 1 - (1 - bonus) * (1 - value)
        for buff in self.s["buffs"]:
            if isinstance(buff.get("技能"), str):
                continue
            value = buff.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bonus = 1 - (1 - bonus) * (1 - value)
        return bonus

    def get_mods_bonus(self, mods: list, attribute: str, prefix: str = "角色") -> float:
        scope = self.scope_of(prefix)
        bonus = 0.0
        if prefix == "角色" or not attribute.startswith(prefix):
            for mod in mods:
                if scope and mod.get("类型") != scope:
                    continue
                value = mod.get(attribute)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    bonus += value
        return bonus

    def bonus_vector(self) -> list[float]:
        return [self.get_total(a) for a in CHARACTER_BONUS]

    # ---------- MOD 条件 ----------
    def condition_values(self) -> dict:
        values: dict = {}
        weapons = []
        if not self.s["meleeWeapon"].get("_isEmpty"):
            weapons.append(self.s["meleeWeapon"])
        if not self.s["rangedWeapon"].get("_isEmpty"):
            weapons.append(self.s["rangedWeapon"])
        if self.s.get("skillWeapon") and not self.s["skillWeapon"].get("inherit"):
            weapons.append(self.s["skillWeapon"])
        for weapon in weapons:
            category = weapon.get("类别")
            if category:
                values[category] = values.get(category, 0) + 1
        for category in self.s.get("teamWeaponCategories") or []:
            values[category] = values.get(category, 0) + 1
        return values

    @staticmethod
    def _check_op(actual, op: str, value) -> bool:
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

    def apply_condition(self, attrs: dict, mods: list) -> bool:
        """复刻 CharBuild.applyCondition：条件改写 MOD 实例（entities.mod_apply_condition）。"""
        cond_values = self.condition_values()
        char_mods = [m for m in self.s["charMods"] if m] + ([self.s["auraMod"]] if self.s.get("auraMod") else [])
        changed = False
        for mod in mods:
            changed = entities.mod_apply_condition(mod, attrs, char_mods, cond_values) or changed
        if changed:
            self._table = None
            self._mods_mul = {}
            self._buffs_mul = {}
        return changed

    # ---------- BUFF 动态 attr ----------
    def apply_buff_attr(self, attrs: dict) -> bool:
        changed = False
        for buff in self.s["buffs"]:
            expressions = buff.get("attr")
            if not expressions:
                continue
            ctx = self.damage_context(attrs)
            for key, expression in expressions.items():
                try:
                    value = evaluate(expression, dict(attrs), None, None, None, ctx.panels, ctx, None, None, None, None)
                except Exception:
                    continue
                value = value if (isinstance(value, float) and math.isfinite(value)) else 0.0
                value = value * _num(buff.get("_coverage", 1))
                if buff.get(key) != value:
                    buff[key] = value
                    changed = True
        return changed

    # ---------- 角色属性 ----------
    def calculate_attributes(self, nocode: bool = False, attr_applied: bool = False, _depth: int = 0) -> dict:
        if _depth > 6:
            raise RecursionError("属性不动点迭代过深")
        bonuses = self.bonus_vector()
        idx = {name: i for i, name in enumerate(CHARACTER_BONUS)}
        attack_bonus = bonuses[idx["攻击"]]
        attack_add = bonuses[idx["固定攻击"]]
        health_add = bonuses[idx["固定生命"]]
        health_bonus = bonuses[idx["生命"]]
        shield_bonus = bonuses[idx["护盾"]]
        defense_bonus = bonuses[idx["防御"]]
        sanity_bonus = bonuses[idx["神智"]]
        elem_bonus = bonuses[idx["属性攻击"]]
        power = 1 + bonuses[idx["技能威力"]]
        durability = 1 + bonuses[idx["技能耐久"]]
        efficiency = 1 + bonuses[idx["技能效益"]]
        scope_range = 1 + bonuses[idx["技能范围"]]
        boost = bonuses[idx["昂扬"]]
        desperate = bonuses[idx["背水"]]
        damage_inc = bonuses[idx["增伤"]]
        elem_inc = bonuses[idx["元素增伤"]]
        phys_inc = bonuses[idx["物理增伤"]]
        weapon_dmg = bonuses[idx["武器伤害"]]
        skill_dmg = bonuses[idx["技能伤害"]]
        skill_speed = bonuses[idx["技能速度"]]
        penetration = bonuses[idx["属性穿透"]]
        imbalance_bonus = bonuses[idx["失衡易伤"]]
        skill_add = bonuses[idx["技能倍率加数"]]
        inherit_ratio = 1 + bonuses[idx["召唤物属性继承比例"]]
        summon_as = bonuses[idx["召唤物攻击速度"]]
        summon_range = bonuses[idx["召唤物范围"]]
        summon_dmg = bonuses[idx["召唤物伤害"]]
        summon_ind = bonuses[idx["召唤物独立增伤"]]
        ignore_def = self.get_total("无视防御")
        skill_ignore_def = self.get_total("技能无视防御")
        ind_inc = self.get_total_mul("独立增伤")
        damage_reduce = self.get_total_reduce("减伤")
        skill_set = bonuses[idx["技能倍率赋值"]]
        skill_mul = self.get_total_mul("技能倍率乘数")
        convert = {k: bonuses[idx[k]] for k in ("转切割", "转贯穿", "转震荡", "转灾厄", "转充盈", "转属克", "转属逆")}
        fullness_bonus = bonuses[idx["充盈威力"]]
        skill_trigger = bonuses[idx["技能触发"]]
        anomaly = bonuses[idx["异常数量"]]
        pet_cd_reduce = bonuses[idx["魔灵CD缩减"]]
        char = self.s["char"]
        mod_attr_bonus = self.get_total(f"{char.get('属性')}MOD属性")
        if mod_attr_bonus > 0:
            elm = [m for m in self.s["charMods"] if m and m.get("系列") in ELM_SERIES]
            attack_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "攻击")
            health_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "生命")
            shield_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "护盾")
            defense_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "防御")
            sanity_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "神智")
            elem_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "属性攻击")
            power += mod_attr_bonus * self.get_mods_bonus(elm, "技能威力")
            durability += mod_attr_bonus * self.get_mods_bonus(elm, "技能耐久")
            efficiency += mod_attr_bonus * self.get_mods_bonus(elm, "技能效益")
            scope_range += mod_attr_bonus * self.get_mods_bonus(elm, "技能范围")
            boost += mod_attr_bonus * self.get_mods_bonus(elm, "昂扬")
            desperate += mod_attr_bonus * self.get_mods_bonus(elm, "背水")
            damage_inc += mod_attr_bonus * self.get_mods_bonus(elm, "增伤")
            elem_inc += mod_attr_bonus * self.get_mods_bonus(elm, "元素增伤")
            phys_inc += mod_attr_bonus * self.get_mods_bonus(elm, "物理增伤")
            weapon_dmg += mod_attr_bonus * self.get_mods_bonus(elm, "武器伤害")
            skill_dmg += mod_attr_bonus * self.get_mods_bonus(elm, "技能伤害")
            skill_speed += mod_attr_bonus * self.get_mods_bonus(elm, "技能速度")
            penetration += mod_attr_bonus * self.get_mods_bonus(elm, "属性穿透")
            imbalance_bonus += mod_attr_bonus * self.get_mods_bonus(elm, "失衡易伤")
            skill_add += mod_attr_bonus * self.get_mods_bonus(elm, "技能倍率加数")
            inherit_ratio += mod_attr_bonus * self.get_mods_bonus(elm, "召唤物属性继承比例")
            summon_as += mod_attr_bonus * self.get_mods_bonus(elm, "召唤物攻击速度")
            summon_range += mod_attr_bonus * self.get_mods_bonus(elm, "召唤物范围")
            summon_dmg += mod_attr_bonus * self.get_mods_bonus(elm, "召唤物伤害")
            summon_ind += mod_attr_bonus * self.get_mods_bonus(elm, "召唤物独立增伤")
        resonance = self.s.get("resonanceGain", 0)
        attack = char["基础攻击"] * (1 + attack_bonus + resonance)
        health = char["基础生命"] * (1 + health_bonus + resonance)
        shield = char["基础护盾"] * (1 + shield_bonus + resonance)
        defense = char["基础防御"] * (1 + defense_bonus + resonance)
        sanity = char["基础神智"] * (1 + sanity_bonus)
        attack = attack * (1 + elem_bonus) + attack_add
        health = js_round(health + health_add)
        shield = js_round(shield)
        defense = js_round(defense)
        sanity = js_round(sanity)
        attack = js_round(attack * 100) / 100
        efficiency = min(efficiency, 1.75)
        scope_range = min(scope_range, 2.8)
        durability = min(durability, 4)
        pet_cd = max(0.0, self.s.get("petBaseCd", 0) * (1 - max(0.0, min(1.0, pet_cd_reduce))))
        attrs = {
            "攻击": attack, "生命": health, "护盾": shield, "防御": defense, "神智": sanity,
            "技能威力": power, "技能耐久": durability, "技能效益": efficiency, "技能范围": scope_range,
            "昂扬": boost, "背水": desperate, "增伤": damage_inc, "元素增伤": elem_inc,
            "物理增伤": phys_inc, "属性攻击": elem_bonus, "武器伤害": weapon_dmg, "技能伤害": skill_dmg,
            "独立增伤": ind_inc, "属性穿透": penetration, "无视防御": ignore_def,
            "技能无视防御": skill_ignore_def, "技能速度": skill_speed, "失衡易伤": imbalance_bonus,
            "技能倍率加数": skill_add, "技能倍率乘数": skill_mul,
            "召唤物属性继承比例": inherit_ratio, "召唤物攻击速度": summon_as,
            "召唤物范围": summon_range, "召唤物伤害": summon_dmg, "召唤物独立增伤": summon_ind,
            "减伤": damage_reduce, "技能倍率赋值": skill_set,
            "有效生命": (health / (1 - defense / (300 + defense)) + shield) / (1 - damage_reduce),
            "转切割": convert["转切割"], "转贯穿": convert["转贯穿"], "转震荡": convert["转震荡"],
            "转灾厄": convert["转灾厄"], "转充盈": convert["转充盈"], "转属克": convert["转属克"],
            "转属逆": convert["转属逆"], "充盈威力": fullness_bonus, "技能触发": skill_trigger,
            "异常数量": (1 + anomaly) if char.get("属性") in ("光", "暗") else max(1, anomaly),
            "魔灵CD": pet_cd, "魔灵CD缩减": max(0.0, min(1.0, pet_cd_reduce)),
        }
        cond_mods = [m for m in [*self.s["charMods"], self.s.get("auraMod")] if m and m.get("生效", {}).get("条件")]
        if self.apply_condition(attrs, cond_mods):
            return self.calculate_attributes(nocode, attr_applied, _depth + 1)
        if not attr_applied and self.apply_buff_attr(attrs):
            return self.calculate_attributes(nocode, True, _depth + 1)
        if nocode:
            return attrs
        if self.s["dynamicBuffs"]:
            all_panels = self.all_weapon_panels()
            mod_attrs = self.mod_attr_sums()
            for buff in self.s["dynamicBuffs"]:
                if isinstance(buff.get("技能"), str):
                    continue
                attrs = self.apply_code_buff(buff, attrs, all_panels, mod_attrs)
        return attrs

    # ---------- code BUFF ----------
    def mod_attr_sums(self) -> dict:
        out = {}
        for slot, mods in (
            ("charMods", self.s["charMods"]), ("meleeMods", self.s["meleeMods"]),
            ("rangedMods", self.s["rangedMods"]), ("skillMods", self.s["skillMods"]),
        ):
            sums: dict = {}
            for mod in mods:
                if not mod:
                    continue
                for prop in entities.mod_properties(mod):
                    value = mod.get(prop)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sums[prop] = sums.get(prop, 0) + value
            out[slot] = sums
        return out

    def all_weapon_panels(self, weapon=None, weapon_attrs=None) -> list:
        ordered = [self.selected_weapon(), self.s["meleeWeapon"], self.s["rangedWeapon"], self.s.get("skillWeapon")]
        arg = weapon if weapon is not None else self.selected_weapon()
        out = []
        for w in ordered:
            if not w:
                out.append(None)
                continue
            if arg is not None and w.get("名称") == arg.get("名称"):
                out.append(weapon_attrs)
            else:
                out.append(self.calculate_weapon_attributes(w, True, True).get("weapon"))
        return out

    def apply_code_buff(self, buff: dict, attrs: dict, all_panels: list, mod_attrs: dict) -> dict:
        """复刻 applyDynamicAttr：沙箱装配 + jscode 执行 + 解构回填。"""
        code = buff.get("code")
        if not code:
            return attrs
        char = self.s["char"]
        weapons = [self.selected_weapon(), self.s["meleeWeapon"], self.s["rangedWeapon"], self.s.get("skillWeapon")]
        panels = list(all_panels) + [None] * (4 - len(all_panels))
        sandbox: dict = dict(attrs)

        def info(w):
            if not w:
                return None
            return {"基础攻击": w.get("基础攻击"), "基础暴击": w.get("基础暴击"), "基础暴伤": w.get("基础暴伤"), "基础触发": w.get("基础触发")}

        sandbox["char"] = {
            "基础攻击": char.get("基础攻击"), "基础生命": char.get("基础生命"),
            "基础护盾": char.get("基础护盾"), "基础防御": char.get("基础防御"),
            "基础神智": char.get("基础神智"),
        }
        names = ("weapon", "meleeWeapon", "rangedWeapon", "skillWeapon")
        for key, w in zip(names, weapons):
            sandbox[key] = info(w)
        for key, panel in zip(("weaponAttr", "meleeWeaponAttr", "rangedWeaponAttr", "skillWeaponAttr"), panels):
            sandbox[key] = panel
        sandbox["enemy"] = self.enemy_sandbox()
        for slot, sums in mod_attrs.items():
            sandbox[slot] = ZeroDict(sums)
        try:
            run_js_code(code, sandbox)
        except Exception:
            pass
        for key in (*names, "weaponAttr", "meleeWeaponAttr", "rangedWeaponAttr", "skillWeaponAttr", "enemy", "char",
                    "charMods", "meleeMods", "rangedMods", "skillMods"):
            sandbox.pop(key, None)
        result = dict(sandbox)
        result["weapon"] = panels[0]
        return result

    def enemy_sandbox(self) -> dict:
        enemy = self.s["enemy"]
        return {"等级": enemy.get("_等级", 80), "def": enemy.get("def", 0)}

    # ---------- 武器属性 ----------
    def selected_weapon(self):
        base = self.s.get("baseName")
        melee_names = {s.get("名称") for s in self.s.get("meleeWeaponSkills", [])}
        ranged_names = {s.get("名称") for s in self.s.get("rangedWeaponSkills", [])}
        skill_names = {s.get("名称") for s in self.s.get("skillWeaponSkills", [])}
        if base in melee_names:
            return self.s["meleeWeapon"]
        if base in ranged_names:
            return self.s["rangedWeapon"]
        if base in skill_names or (self.s.get("skillWeapon") and self.s["skillWeapon"].get("名称") == base):
            return self.s.get("skillWeapon")
        return None

    def calculate_weapon_attributes(self, weapon=None, nocode: bool = False, nochar: bool = False) -> dict:
        if weapon is None:
            weapon = self.selected_weapon()
            if weapon is None:
                selected_skill = next((s for s in self.s["allSkills"] if s.get("名称") == self.s.get("baseName")), None)
                if selected_skill and selected_skill.get("召唤物"):
                    weapon = self.s["meleeWeapon"]
        if weapon is not None and weapon.get("inherit"):
            weapon = self.s["meleeWeapon"] if weapon["inherit"] == "melee" else self.s["rangedWeapon"]
        attrs = {} if nochar else self.calculate_attributes(True)
        if weapon:
            prefix = weapon.get("类型", "")
            attack_bonus = self.get_total(f"{prefix}攻击", prefix) + self.get_total("攻击", prefix)
            physical_bonus = self.get_total("物理", prefix)
            crit_bonus = self.get_total(f"{prefix}暴击", prefix) + self.get_total("暴击", prefix)
            crit_dmg_bonus = self.get_total(f"{prefix}暴伤", prefix) + self.get_total("暴伤", prefix)
            trig_bonus = self.get_total(f"{prefix}触发", prefix) + self.get_total("触发", prefix)
            as_bonus = self.get_total(f"{prefix}攻速", prefix) + self.get_total("攻速", prefix)
            multi_bonus = self.get_total(f"{prefix}多重", prefix) + self.get_total("多重", prefix)
            dmg_inc = self.get_total(f"{prefix}增伤", prefix) + self.get_total("增伤", prefix)
            reload_bonus = self.get_total(f"{prefix}装填", prefix) + self.get_total("装填", prefix)
            mag_bonus = self.get_total(f"{prefix}弹匣", prefix) + self.get_total("弹匣", prefix)
            ammo_bonus = self.get_total(f"{prefix}弹药", prefix) + self.get_total("弹药", prefix)
            additional = self.get_total("追加伤害")
            weapon_mul = self.get_total(f"{prefix}武器倍率", prefix) + self.get_total("武器倍率", prefix)
            ind_inc = (1 + self.get_total_mul(f"{prefix}独立增伤", prefix)) * (1 + self.get_total_mul("独立增伤", prefix)) - 1
            char = self.s["char"]
            mod_attr_bonus = self.get_total(f"{char.get('属性')}MOD属性")
            if mod_attr_bonus > 0:
                elm = [m for m in self.s["charMods"] if m and m.get("系列") in ELM_SERIES]
                additional += mod_attr_bonus * self.get_mods_bonus(elm, "追加伤害")
            if prefix.startswith("同律"):
                lower = prefix[2:]
                attack_bonus += self.get_total(f"{lower}攻击", lower, False)
                crit_bonus += self.get_total(f"{lower}暴击", lower, False)
                crit_dmg_bonus += self.get_total(f"{lower}暴伤", lower, False)
                trig_bonus += self.get_total(f"{lower}触发", lower, False)
                as_bonus += self.get_total(f"{lower}攻速", lower, False)
                dmg_inc += self.get_total(f"{lower}增伤", lower, False)
                multi_bonus += self.get_total(f"{lower}多重", lower, False)
                reload_bonus += self.get_total(f"{lower}装填", lower, False)
                mag_bonus += self.get_total(f"{lower}弹匣", lower, False)
                ammo_bonus += self.get_total(f"{lower}弹药", lower, False)
                weapon_mul += self.get_total(f"{lower}武器倍率", lower, False)
                ind_inc = (1 + ind_inc) * (1 + self.get_total_mul(f"{lower}独立增伤", lower, False)) - 1
            as_bonus = min(as_bonus, 2)
            mastered = self.mastered(weapon.get("类别", ""))
            atk_ratio = (1.4 if str(prefix).startswith("同律") else 1.2) if mastered else 1
            attack = weapon.get("基础攻击", 0) * (1 + attack_bonus) * atk_ratio
            crit = weapon.get("基础暴击", 0) * (1 + crit_bonus)
            crit_dmg = weapon.get("基础暴伤", 0) * (1 + crit_dmg_bonus)
            trig = weapon.get("基础触发", 0) * (1 + trig_bonus)
            attack_speed = (weapon.get("射速") or 1) * (1 + as_bonus)
            reload = (weapon.get("基础装填") or 0) / (1 + reload_bonus)
            magazine = (weapon.get("基础弹匣") or 0) * (1 + mag_bonus)
            ammo = (weapon.get("基础弹药") or 0) * (1 + ammo_bonus)
            multi = 1 + multi_bonus
            attack *= 1 + physical_bonus
            attack = js_round(attack * 100) / 100
            crit = js_round(crit * 100) / 100
            crit_dmg = js_round(crit_dmg * 100) / 100
            trig = js_round(trig * 100) / 100
            attack_speed = js_round(attack_speed * 100) / 100
            multi = js_round(multi * 100) / 100
            ind_inc = js_round(ind_inc * 1000) / 1000
            attrs["weapon"] = {
                "攻击": attack, "暴击": crit, "暴伤": crit_dmg, "触发": trig,
                "攻速": attack_speed, "多重": multi, "增伤": dmg_inc, "独立增伤": ind_inc,
                "追加伤害": additional, "装填": reload, "弹匣": magazine, "弹药": ammo,
                "武器倍率": weapon_mul,
                "充盈转化": 1 + self.get_total("充盈转化", prefix),
                "召唤物攻击速度转化": self.get_total("召唤物攻击速度转化", prefix),
                "召唤物范围转化": self.get_total("召唤物范围转化", prefix),
            }
        if nochar:
            return attrs
        fullness = 0.0
        for w in self.fullness_weapons():
            trig_rate, conv = self.weapon_fullness(w)
            fullness += max(0.0, trig_rate - 1) * conv
        attrs["充盈威力"] = (attrs.get("充盈威力") or 0) + fullness
        melee = self.s["meleeWeapon"]
        if melee and not melee.get("_isEmpty"):
            attack_speed, conv = self.weapon_summon_speed(melee)
            attrs["召唤物攻击速度"] = (attrs.get("召唤物攻击速度") or 0) + max(0.0, attack_speed) * conv
            attrs["召唤物范围"] = (attrs.get("召唤物范围") or 0) + self.get_total("召唤物范围转化", "近战")
        if nocode:
            return attrs
        if self.s["dynamicBuffs"]:
            all_panels = self.all_weapon_panels(weapon, attrs.get("weapon"))
            mod_attrs = self.mod_attr_sums()
            for buff in self.s["dynamicBuffs"]:
                if isinstance(buff.get("技能"), str):
                    continue
                attrs = self.apply_code_buff(buff, attrs, all_panels, mod_attrs)
        return attrs

    def fullness_weapons(self) -> list:
        out = []
        if self.s["meleeWeapon"] and not self.s["meleeWeapon"].get("_isEmpty"):
            out.append(self.s["meleeWeapon"])
        if self.s["rangedWeapon"] and not self.s["rangedWeapon"].get("_isEmpty"):
            out.append(self.s["rangedWeapon"])
        if self.s.get("skillWeapon") and not self.s["skillWeapon"].get("inherit"):
            out.append(self.s["skillWeapon"])
        return out

    def weapon_fullness(self, weapon: dict):
        prefix = weapon.get("类型", "")
        bonus = self.get_total(f"{prefix}触发", prefix) + self.get_total("触发", prefix)
        if prefix.startswith("同律"):
            lower = prefix[2:]
            bonus += self.get_total(f"{lower}触发", lower, False)
        rate = js_round(weapon.get("基础触发", 0) * (1 + bonus) * 100) / 100
        # 充盈转化含每把武器固定的基础转化 1，再叠加对应作用域 MOD 转化
        return rate, 1 + self.get_total("充盈转化", prefix)

    def weapon_summon_speed(self, weapon: dict):
        prefix = weapon.get("类型", "")
        bonus = self.get_total(f"{prefix}攻速", prefix) + self.get_total("攻速", prefix)
        if prefix.startswith("同律"):
            lower = prefix[2:]
            bonus += self.get_total(f"{lower}攻速", lower, False)
        return (weapon.get("射速") or 1) * (1 + min(bonus, 2)), self.get_total("召唤物攻击速度转化", prefix)

    # ---------- 面板/技能/战斗装配 ----------
    def weapon_panels(self) -> dict:
        panels = {}
        melee, ranged = self.s["meleeWeapon"], self.s["rangedWeapon"]
        panels["远程"] = self.calculate_weapon_attributes(ranged, True, True).get("weapon")
        panels["近战"] = self.calculate_weapon_attributes(melee, True, True).get("weapon")
        skill_weapon = self.s.get("skillWeapon")
        if skill_weapon:
            inherit = skill_weapon.get("inherit")
            if inherit == "melee":
                panels["同律"] = panels["近战"]
            elif inherit == "ranged":
                panels["同律"] = panels["远程"]
            else:
                panels["同律"] = self.calculate_weapon_attributes(skill_weapon, True, True).get("weapon")
        panels["melee"] = panels["近战"]
        panels["ranged"] = panels["远程"]
        if "同律" in panels:
            panels["skill"] = panels["同律"]
        for skill in self.s.get("weaponSkills", []):
            slot = (skill.get("武器") or "")[:2]
            if slot and slot in panels:
                panels[skill["名称"]] = panels[slot]
        return panels

    def weapons_keys(self) -> list[str]:
        keys = ["近战", "远程"]
        if self.s.get("skillWeapon"):
            keys.append("同律")
        keys += ["melee", "ranged"]
        if self.s.get("skillWeapon"):
            keys.append("skill")
        for skill in self.s.get("weaponSkills", []):
            slot = (skill.get("武器") or "")[:2]
            if slot and slot in ("近战", "远程", "同律"):
                keys.append(skill["名称"])
        return keys

    def skill_tables(self, attrs: dict) -> dict:
        import re as _re

        rules = []
        for rule in self.conditional_list():
            try:
                rules.append((_re.compile(rule["pattern"]), rule["props"]))
            except _re.error:
                rules.append((_re.compile(_re.escape(rule["pattern"])), rule["props"]))
        tables = {}
        for skill in self.s["allSkills"]:
            tables[skill["safeName"]] = entities.level_skill_fields_with_attr(skill, attrs, rules)
        # E/Q/P（含小写）别名与 TS 求值上下文一致
        e_alias = self.s.get("skill_aliases") or {}
        for alias in ("E", "e", "Q", "q", "P", "p"):
            safe = e_alias.get(alias.upper())
            if safe and safe in tables:
                tables[alias] = tables[safe]
        return tables

    def conditional_list(self) -> list:
        """复刻 getConditionalBuffPropsList（含 code 条件差分，pattern 保留源码）。"""
        grouped: dict = {}
        order: list = []
        for buff in self.s["buffs"]:
            pattern = buff.get("技能")
            if not isinstance(pattern, str):
                continue
            if pattern not in grouped:
                grouped[pattern] = {}
                order.append(pattern)
            for prop in entities.buff_properties(buff):
                value = buff.get(prop)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0:
                    continue
                if prop in ("无视防御", "技能无视防御", "技能倍率乘数") or prop.endswith("独立增伤"):
                    grouped[pattern][prop] = (1 + (grouped[pattern].get(prop) or 0)) * (1 + value) - 1
                else:
                    grouped[pattern][prop] = (grouped[pattern].get(prop) or 0) + value
        rules = [{"技能": p, "pattern": p, "flags": "", "props": grouped[p]} for p in order]
        rules.extend(self._code_conditionals())
        return rules

    def _code_conditionals(self) -> list:
        scoped = [b for b in self.s["dynamicBuffs"] if isinstance(b.get("技能"), str)]
        if not scoped:
            return []
        base = self.calculate_weapon_attributes()
        out = []
        for buff in scoped:
            snapshot = dict(base)
            result = self.apply_code_buff(buff, snapshot, self.all_weapon_panels(), self.mod_attr_sums())
            props = {}
            for key, value in result.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                before = snapshot.get(key)
                if not isinstance(before, (int, float)) or isinstance(before, bool) or value == before:
                    continue
                if key in ("无视防御", "技能无视防御", "技能倍率乘数") or key.endswith("独立增伤"):
                    if abs(1 + before) < 1e-12:
                        continue
                    props[key] = (1 + value) / (1 + before) - 1
                else:
                    props[key] = value - before
            out.append({"技能": buff["技能"], "pattern": buff["技能"], "flags": "", "props": props})
        return out

    def combat_info(self) -> dict:
        enemy = self.s["enemy"]
        return {
            "enemyDef": enemy.get("def", 0),
            "enemyLevel": enemy.get("_等级", 80),
            "enemyShield": enemy.get("currentShield", 0),
            "currentHPType": "护盾" if (enemy.get("currentShield") or 0) > 0 else "生命",
            "resistance": self.s.get("enemyResistance", 0) or 0,
            "triggerBonus": self.get_total("触发倍率"),
            "imbalance": bool(self.s.get("imbalance")),
            "charLevel": self.s["char"].get("_等级", 80),
            "hpPercent": self.s.get("hpPercent", 1),
            "hpTypeCoefficients": dict(HP_TYPE_COEFFICIENTS),
            "hpTypeDMG": dict(HP_TYPE_DMG),
            "hasArrowRainMod": any(m and m.get("id") == 43604 for m in self.s["rangedMods"]),
        }

    def weapons_info(self) -> dict:
        info = {}
        by_name = {}
        for w in (self.s["meleeWeapon"], self.s["rangedWeapon"], self.s.get("skillWeapon")):
            if w:
                by_name[w.get("名称")] = w
        panels = self.weapon_panels()
        for key in self.weapons_keys():
            inst = None
            if key in ("近战", "melee"):
                inst = self.s["meleeWeapon"]
            elif key in ("远程", "ranged"):
                inst = self.s["rangedWeapon"]
            elif key in ("同律", "skill"):
                inst = self.s.get("skillWeapon")
            else:
                for skill in self.s.get("weaponSkills", []):
                    if skill.get("名称") == key:
                        slot = (skill.get("武器") or "")[:2]
                        inst = {"近战": self.s["meleeWeapon"], "远程": self.s["rangedWeapon"], "同律": self.s.get("skillWeapon")}.get(slot)
                        break
            if not inst:
                info[key] = None
                continue
            info[key] = {
                "伤害类型": inst.get("伤害类型"),
                "类型": inst.get("类型"),
                "inherit": inst.get("inherit"),
                "atk": inst.get("atk"),
                "视为": inst.get("视为"),
                "isSkillWeapon": bool(inst.get("_isSkillWeapon")),
            }
        return info

    def weapon_bases(self) -> dict:
        picked = ("基础攻击", "基础暴击", "基础暴伤", "基础触发", "射速", "基础装填", "基础弹匣", "基础弹药")
        out = {}
        for key in self.weapons_keys():
            inst = None
            if key in ("近战", "melee"):
                inst = self.s["meleeWeapon"]
            elif key in ("远程", "ranged"):
                inst = self.s["rangedWeapon"]
            elif key in ("同律", "skill"):
                sw = self.s.get("skillWeapon")
                if sw and sw.get("inherit"):
                    inst = self.s["meleeWeapon"] if sw["inherit"] == "melee" else self.s["rangedWeapon"]
                else:
                    inst = sw
            else:
                continue
            out[key] = {k: inst.get(k) if inst else None for k in picked} if inst else None
        return out

    def field_tags(self, base_name: str, field_name: str | None, ctx_safe: str | None) -> list | None:
        """复刻 getFieldTags：技能表优先，未命中回退武器自身技能字段。"""
        if not field_name:
            return None
        tables = getattr(self, "_skill_tables_cache", None)
        skill = None
        if ctx_safe and tables:
            fields = tables.get(ctx_safe) or []
            field = next((f for f in fields if field_name in (f.get("safeName") or "") or field_name in (f.get("名称") or "")), None)
            if field and field.get("tag"):
                return list(field["tag"])
        if tables:
            for entry in self.s.get("skill_name_list", []):
                if entry.get("名称") == base_name and tables.get(entry.get("safeName")):
                    field = next(
                        (f for f in tables[entry["safeName"]] if field_name in (f.get("safeName") or "") or field_name in (f.get("名称") or "")),
                        None,
                    )
                    if field and field.get("tag"):
                        return list(field["tag"])
        return None

    def attack_type_bonus(self, weapon_type: str, prefix: str, attribute: str) -> float:
        """复刻 getWeaponAttackTypeBonus 的加成汇总段（前缀基为 weapon.类型，细分前缀已解出）。"""
        scope = self.scope_of(weapon_type)
        multiplicative = attribute.endswith("独立增伤")

        def bonus(attr: str, pre: str, include_mods: bool = True) -> float:
            if multiplicative:
                return self.get_total_mul(attr, pre, include_mods)
            return self.get_total(attr, pre, include_mods)

        total = bonus(f"{weapon_type}{prefix}{attribute}", scope)
        if "近战" in weapon_type:
            total += bonus(f"{prefix}{attribute}", scope)
        if weapon_type.startswith("同律"):
            lower = weapon_type[2:]
            lower_scope = self.scope_of(lower)
            total += bonus(f"{lower}{prefix}{attribute}", lower_scope, False)
            if "近战" in lower:
                total += bonus(f"{prefix}{attribute}", lower_scope, False)
            total += self.mods_scope_bonus(f"{lower}{prefix}{attribute}", scope, multiplicative)
        return total

    def mods_scope_bonus(self, attribute: str, scope: str, multiplicative: bool = False) -> float:
        table = self.bonus_table()
        if not multiplicative:
            if scope:
                return (table["modsByScope"].get(scope) or {}).get(attribute, 0)
            return table["modsAll"].get(attribute, 0)
        product = 1.0
        for mod in self.scoped_mods(scope):
            value = mod.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                product *= 1 + value
        return product - 1

    # ---------- DamageContext 装配 ----------
    def _instance_for_key(self, key: str):
        if key in ("近战", "melee"):
            return self.s["meleeWeapon"]
        if key in ("远程", "ranged"):
            return self.s["rangedWeapon"]
        if key in ("同律", "skill"):
            return self.s.get("skillWeapon")
        for skill in self.s.get("weaponSkills", []):
            if skill.get("名称") == key:
                slot = (skill.get("武器") or "")[:2]
                return {"近战": self.s["meleeWeapon"], "远程": self.s["rangedWeapon"], "同律": self.s.get("skillWeapon")}.get(slot)
        return None

    def context_panels(self, current: dict) -> dict:
        """求值用面板：独立面板 + 命中选中武器的键回填全量面板（含 code 修正）。"""
        panels = self.weapon_panels()
        selected = self.selected_weapon()
        selected_panel = (current or {}).get("weapon")
        if selected is not None and isinstance(selected_panel, dict):
            for key in self.weapons_keys():
                if self._instance_for_key(key) is selected:
                    panels[key] = selected_panel
        return panels

    def damage_context(self, attrs: dict | None = None) -> DamageContext:
        current = attrs if attrs is not None else self.calculate_weapon_attributes()
        tables = self.skill_tables(current)
        self._skill_tables_cache = tables
        engine = self
        panels = self.context_panels(current)

        def bonus_fn(key, field_name, attribute, weapon_type=None, tags=None, ctx_safe=None):
            real_tags = tags if tags is not None else engine.field_tags(key, field_name, ctx_safe)
            prefix = None
            for name, names in ATTACK_TYPE_TAGS:
                if real_tags and any(t in real_tags for t in names):
                    prefix = name
                    break
            if prefix is None:
                view = None
                if key in ("近战", "melee"):
                    view = engine.s["meleeWeapon"].get("视为")
                elif key in ("远程", "ranged"):
                    view = engine.s["rangedWeapon"].get("视为")
                else:
                    sw = engine.s.get("skillWeapon")
                    view = (sw or {}).get("视为")
                if view:
                    for name, names in ATTACK_TYPE_TAGS:
                        if view in names or view == name:
                            prefix = name
                            break
            if prefix is None:
                return 0.0
            return engine.attack_type_bonus(weapon_type or key, prefix, attribute)

        rules = [{"技能": r["技能"], "pattern": r["pattern"], "flags": r.get("flags", ""), "props": r["props"]} for r in self.conditional_list()]
        ctx = DamageContext(
            attrs=current,
            panels=panels,
            skill_tables=tables,
            skill_names=self.s.get("skill_name_list", []),
            aliases=self.s.get("skill_aliases", {}),
            rules=rules,
            attack_bonus={},
            weapons=self.weapons_info(),
            weapon_bases=self.weapon_bases(),
            combat=self.combat_info(),
            base_name=self.s.get("baseName", ""),
            attack_bonus_fn=bonus_fn,
        )
        return ctx

    # ---------- 面板输出 ----------
    def char_panel(self) -> dict:
        """角色面板：指定等级 + MOD 列表下的全量角色属性（不含 weapon 键）。"""
        attrs = self.calculate_weapon_attributes()
        return {k: v for k, v in attrs.items() if k != "weapon"}

    def weapon_panel(self, slot: str = "melee") -> dict | None:
        """武器面板：指定槽位武器的面板数值。

        slot 取 melee（近战）/ ranged（远程）/ skill（同律），或武器技能名/baseName。
        """
        norm = {"melee": "近战", "ranged": "远程", "skill": "同律"}.get(slot, slot)
        if norm in ("近战", "melee"):
            weapon = self.s["meleeWeapon"]
        elif norm in ("远程", "ranged"):
            weapon = self.s["rangedWeapon"]
        elif norm in ("同律", "skill"):
            weapon = self.s.get("skillWeapon")
        else:
            weapon = None
            for skill in self.s.get("weaponSkills", []):
                if skill.get("名称") == norm:
                    weapon = self._instance_for_key(norm)
                    break
            if weapon is None:
                weapon = next((w for w in (self.s["meleeWeapon"], self.s["rangedWeapon"], self.s.get("skillWeapon")) if w and w.get("名称") == norm), None)
        if not weapon:
            return None
        return self.calculate_weapon_attributes(weapon, True, True).get("weapon")

    def skill_level_at(self, index: int) -> int:
        """复刻 getSkillLevel：按角色技能索引取等级（0→E，1→Q，2 及之后→被动）。"""
        from .build import resolve_skill_level

        return resolve_skill_level(self.s.get("skillLevel", [10, 10, 10]), index)

    def selected_skill_level(self) -> int:
        """复刻 selectedSkillLevel：选中技能在角色技能中的索引解析等级。"""
        from .build import resolve_skill_level

        index = next((i for i, s in enumerate(self.s["skills"]) if s.get("名称") == self.s.get("baseName")), -1)
        return resolve_skill_level(self.s.get("skillLevel", [10, 10, 10]), index if index >= 0 else 2)

    def skill_levels_final(self) -> list:
        """最终技能等级：[(技能名, 等级)]（前三取 trio + 溯源加成并钳制 1-12）。"""
        return [(s.get("名称"), self.s["skillLevelsFinal"][i] if i < len(self.s["skillLevelsFinal"]) else None) for i, s in enumerate(self.s["skills"])]

    def skill_fields(self, skill_name: str) -> list:
        """技能面板数值（字段）：最终属性下该技能的结算字段表。"""
        attrs = self.calculate_weapon_attributes()
        ctx = self.damage_context(attrs)
        safe = next((e.get("safeName") for e in (self.s.get("skill_name_list") or []) if e.get("名称") == skill_name or e.get("safeName") == skill_name), None)
        if safe is None:
            return []
        return [{k: f.get(k) for k in ("名称", "safeName", "值", "值2", "格式", "基础", "tag", "伤害类型") if k in f} for f in (ctx.tables.get(safe) or [])]

    # ---------- 求值 ----------
    def custom_tables(self) -> tuple[dict, dict]:
        variables: dict = {}
        functions: dict = {}
        for key, value in self.s.get("customVariables") or []:
            text_key = (key or "").strip()
            text_value = (value or "").strip()
            if not text_key or not text_value:
                continue
            if not _valid_var_key(text_key):
                continue
            parsed = _parse_func_def(text_key)
            if parsed:
                functions[parsed[0]] = (parsed[1], text_value)
            else:
                variables[text_key] = text_value
        return variables, functions

    def calculate(self, target: str | None = None) -> float:
        """复刻 calculateOneTime（无时间线：护盾为 0 即目标函数值）。"""
        enemy = self.s["enemy"]
        enemy["currentHP"] = enemy.get("hp", 0)
        enemy["currentShield"] = enemy.get("es") or 0
        attrs = self.calculate_weapon_attributes()
        ctx = self.damage_context(attrs)
        variables, functions = self.custom_tables()
        result = evaluate(
            target or self.s.get("targetFunction") or "伤害",
            attrs, None, variables, functions, ctx.panels, ctx,
        )
        return result if isinstance(result, float) and math.isfinite(result) else 0.0


_FUNC_DEF_RE = re.compile(r"^([a-zA-Z_\u4e00-\u9fa5·\[][a-zA-Z0-9_\u4e00-\u9fa5·\]]*)\s*\(([^()]*)\)\s*$")
_VAR_NAME_RE = re.compile(r"^[a-zA-Z_\u4e00-\u9fa5·\[][a-zA-Z0-9_\u4e00-\u9fa5·\]]*$")
_PARAM_RE = re.compile(r"^[a-zA-Z_\u4e00-\u9fa5·][a-zA-Z0-9_\u4e00-\u9fa5·\]]*$")


def _parse_func_def(key: str):
    """复刻 parseCustomFunctionDefinition。"""
    match = _FUNC_DEF_RE.match(key.strip())
    if not match:
        return None
    params = [p.strip() for p in match.group(2).split(",") if p.strip()]
    return match.group(1), params


def _valid_var_key(key: str) -> bool:
    """复刻 validateCustomVariableKey（非法键的变量不进入求值表）。"""
    text = key.strip()
    if not text or "::" in text or "." in text:
        return False
    parsed = _parse_func_def(text)
    if parsed:
        name, params = parsed
        if not _VAR_NAME_RE.match(name):
            return False
        seen = set()
        for param in params:
            if not _PARAM_RE.match(param) or param in seen:
                return False
            seen.add(param)
        return True
    if not _VAR_NAME_RE.match(text):
        return False
    try:
        node = parse_ast(text)
    except Exception:
        return False
    return node.get("type") == "property" and node.get("name") == text and not node.get("namespace")
