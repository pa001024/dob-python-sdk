"""技能伤害结算链：`CharBuild.evaluateSkill/getDamage/getDef` 的纯数据复刻。

设计原则：凡是来自构筑聚合（BUFF/MOD/武器加成表）的深度求和，一律由 oracle
（bun 真机）导出为确定表；Python 只复刻确定性的结算公式。无编辑语义。

oracle 输入（见 tests/golden/*.json，由 .tmp/dump-shardbuild-golden.ts 生成）：
- skill_tables：`safeName → getFieldsWithAttr` 快照（与求值上下文同一口径）
- skill_names / skill_aliases：`{名称, safeName}` 与 E/Q/P 别名
- conditional_rules：条件 BUFF `{技能, pattern源码, props}`（Python 侧 re 编译）
- attack_bonus_log：`getWeaponAttackTypeBonus` 真机调用日志 `key::field::attr → 值`
- weapons / weapon_bases：面板键 → `{伤害类型, 类型, inherit, atk, isSkillWeapon}` 与有效武器基础值
- combat：敌人/战斗状态；attrs / weapon_panels 沿用 evaluate 口径

TS 出处（行号指 src/data/CharBuild.ts）：getWeaponFieldBase≈3185、
getTemporaryWeaponAttr≈3233、getSummonAttrs≈3260、isDamageSkillField≈3323、
getDamage≈3342、getDef≈3407、evaluateExpression≈3427、calculateSkillDamage≈2220、
calculateWeaponDamage≈2298、calculateDefenseMultiplier≈2182。
"""

from __future__ import annotations

import math
import re

from .ast import AstError, parse_ast

WEAPON_DAMAGE_FIELD_BASE = {"[近战]": "近战", "[远程]": "远程", "[同律]": "同律"}
PHYSICAL_CONVERTS = ["转切割", "转贯穿", "转震荡", "转灾厄"]
WEAPON_ATTR_BASES = {
    "攻击": "基础攻击",
    "暴击": "基础暴击",
    "暴伤": "基础暴伤",
    "触发": "基础触发",
    "攻速": "射速",
    "装填": "基础装填",
    "弹匣": "基础弹匣",
    "弹药": "基础弹药",
}
FLAT_MAP = {"固定攻击": "攻击", "固定生命": "生命"}
_ALIAS_IDX = {"E": 0, "e": 0, "Q": 1, "q": 1, "P": 2, "p": 2}
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_DAMAGE_BRANCH_MEMBERS = frozenset(
    [
        "N", "物理", "元素", "暴击", "未暴击", "触发", "未触发",
        "暴击触发", "触发暴击", "未触发暴击", "暴击未触发", "触发未暴击",
        "未暴击触发", "未暴击未触发", "未触发未暴击",
    ]
)


def _num(value) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _is_mult(attr: str) -> bool:
    """对齐 leveled/minusAttr.isMultiplicativeAttr：封闭规则。"""
    return attr in ("无视防御", "技能无视防御", "技能倍率乘数") or attr.endswith("独立增伤")


class DamageContext:
    """一次真机快照的全部确定表 + 结算公式。"""

    def __init__(
        self,
        *,
        attrs: dict,
        panels: dict,
        skill_tables: dict,
        skill_names: list,
        aliases: dict,
        rules: list,
        attack_bonus: dict,
        weapons: dict,
        weapon_bases: dict,
        combat: dict,
        base_name: str,
        attack_bonus_fn=None,
    ):
        self.attrs = attrs
        self.attack_bonus_fn = attack_bonus_fn
        self.panels = panels
        self.tables = skill_tables
        self.skill_names = skill_names
        self.aliases = aliases
        self.attack_bonus = attack_bonus
        self.weapons = weapons
        self.weapon_bases = weapon_bases
        self.combat = combat
        self.base_name = base_name
        self._cond_cache: dict = {}
        self._damage_cache: dict = {}
        self.rules = []
        for rule in rules:
            flags = 0
            for ch in rule.get("flags", ""):
                flags |= _FLAG_MAP.get(ch, 0)
            try:
                self.rules.append((re.compile(rule["pattern"], flags), dict(rule.get("props", {}))))
            except re.error:
                self.rules.append((re.compile(re.escape(rule["pattern"])), dict(rule.get("props", {}))))

    @classmethod
    def from_golden(cls, golden: dict) -> "DamageContext":
        combat = golden["combat"]
        return cls(
            attrs=golden["attrs"],
            panels=golden.get("weaponPanels") or {},
            skill_tables=golden.get("skillTables") or {},
            skill_names=golden.get("skillNames") or [],
            aliases=golden.get("skillAliases") or {},
            rules=golden.get("conditionalRules") or [],
            attack_bonus=golden.get("attackBonusLog") or {},
            weapons=golden.get("weapons") or {},
            weapon_bases=golden.get("weaponBases") or {},
            combat=combat,
            base_name=(golden.get("bdSettings") or {}).get("baseName") or "萨麦尔",
        )

    # ---- 技能定位 ----
    def resolve_skill_safe(self, namespace: str | None) -> str | None:
        """对齐 resolveSkillContext：别名 E/Q/P → skills；否则按 名称/safeName 精确找。"""
        if not namespace:
            return None
        if namespace in _ALIAS_IDX:
            ordered = [self.aliases.get(k) for k in ("E", "Q", "P")]
            safe = ordered[_ALIAS_IDX[namespace] if _ALIAS_IDX[namespace] < 3 else 0]
            return safe
        for entry in self.skill_names:
            if entry.get("名称") == namespace or entry.get("safeName") == namespace:
                return entry.get("safeName")
        return None

    def _table(self, safe: str | None) -> list:
        return self.tables.get(safe) or [] if safe else []

    def find_field(self, fields: list, query: str) -> dict | None:
        """对齐 getSkillAttr：safeName.includes(query)。"""
        for field in fields:
            if query and query in (field.get("safeName") or ""):
                return field
        return None

    def find_field_wide(self, fields: list, query: str) -> dict | None:
        """对齐 getFieldTags/充盈查找：safeName 或 名称 includes(query)。"""
        for field in fields:
            if query and (query in (field.get("safeName") or "") or query in (field.get("名称") or "")):
                return field
        return None

    def get_skill_attr(self, field_name: str, base: str | None, ctx_safe: str | None) -> dict | None:
        if ctx_safe:
            return self.find_field(self._table(ctx_safe), field_name)
        table = self.tables.get(base or self.base_name) or []
        return self.find_field(table, field_name)

    # ---- 条件BUFF ----
    def cond_props(self, field_name: str | None) -> dict | None:
        """对齐 getConditionalBuffProps：pattern 命中即合并（乘法池按 Π-1）。"""
        if not field_name:
            return None
        if field_name in self._cond_cache:
            return self._cond_cache[field_name]
        result: dict | None = None
        for pattern, props in self.rules:
            if not pattern.search(field_name):
                continue
            for prop, value in props.items():
                result = result if result is not None else {}
                if _is_mult(prop):
                    result[prop] = (1 + (result.get(prop) or 0)) * (1 + value) - 1
                else:
                    result[prop] = (result.get(prop) or 0) + value
        self._cond_cache[field_name] = result
        return result

    # ---- 武器字段基 ----
    def weapon_field_base(self, field_name: str | None) -> str | None:
        return WEAPON_DAMAGE_FIELD_BASE.get(field_name or "")

    def weapon_damage_base(self, base: str | None, field_name: str | None, ctx_safe: str | None) -> str | None:
        keyword = self.weapon_field_base(field_name)
        if keyword:
            return keyword
        key = base or self.base_name
        if key not in self.panels or not field_name:
            return None
        table = self._table(ctx_safe) if ctx_safe else (self.tables.get(key) or [])
        field = self.find_field(table, field_name)
        if field and ((field.get("名称") or "").endswith("伤害") or (field.get("名称") or "").endswith("伤害倍率")):
            return key
        return None

    def _panel_for(self, base: str | None) -> dict | None:
        key = base or self.base_name
        panel = self.panels.get(key)
        return panel if isinstance(panel, dict) else None

    def temp_weapon_attr(self, base: str | None, field_name: str | None, temp: dict | None, ctx_safe: str | None) -> dict | None:
        """对齐 getTemporaryWeaponAttr：y += 武器基础值 * x；无 weaponBase/面板/temp 即返回面板。"""
        weapon_base = self.weapon_damage_base(base, field_name, ctx_safe)
        panel = self._panel_for(weapon_base or base)
        if not weapon_base or not panel or not temp:
            return panel
        eff = self.weapon_bases.get(weapon_base) or {}
        out = dict(panel)
        for attribute, value in temp.items():
            if attribute not in panel or not isinstance(panel.get(attribute), (int, float)) or isinstance(panel.get(attribute), bool):
                continue
            base_attr = WEAPON_ATTR_BASES.get(attribute)
            base_value = _num(eff.get(base_attr)) if base_attr else 1.0
            out[attribute] = _num(panel[attribute]) + base_value * _num(value)
        return out

    # ---- 召唤物/临时叠加 ----
    def summon_attrs(self, base: str | None, temp: dict | None, field_name: str | None, ctx_safe: str | None) -> dict:
        """对齐 getSummonAttrs：召唤物继承 → 条件BUFF → 字段临时属性。"""
        key = base or self.base_name
        attrs = self.attrs
        # 召唤物继承：字段 tag 含「召唤物」即按 召唤物属性继承比例 缩放攻击/昂扬/背水
        table_safe = ctx_safe
        if not table_safe:
            for entry in self.skill_names:
                if entry.get("名称") == key:
                    table_safe = entry.get("safeName")
                    break
        summon_field = self.find_field_wide(self._table(table_safe), field_name or "") if field_name else None
        ratio = _num(attrs.get("召唤物属性继承比例", 1)) if summon_field and "召唤物" in (summon_field.get("tag") or []) else 1.0
        current = attrs if ratio == 1 else {**attrs, "攻击": _num(attrs.get("攻击")) * ratio, "昂扬": _num(attrs.get("昂扬")) * ratio, "背水": _num(attrs.get("背水")) * ratio}
        conditional = self.cond_props(field_name) if field_name else None
        scoped = current
        if conditional:
            scoped = dict(current)
            for attribute, value in conditional.items():
                if _is_mult(attribute):
                    if not isinstance(scoped.get(attribute), (int, float)) or isinstance(scoped.get(attribute), bool):
                        continue
                    scoped[attribute] = (1 + _num(scoped[attribute])) * (1 + _num(value)) - 1
                    continue
                target = FLAT_MAP.get(attribute, attribute)
                if not isinstance(scoped.get(target), (int, float)) or isinstance(scoped.get(target), bool):
                    continue
                scoped[target] = _num(scoped[target]) + _num(value)
        if not temp:
            return scoped
        field_attrs = dict(scoped)
        temp_panel = self.temp_weapon_attr(base, field_name, temp, ctx_safe)
        has_weapon_base = bool(self.weapon_damage_base(base, field_name, ctx_safe))
        for attribute, value in temp.items():
            if has_weapon_base and isinstance((temp_panel or {}).get(attribute), (int, float)):
                continue
            target = FLAT_MAP.get(attribute, attribute)
            if target not in attrs or not isinstance(attrs.get(target), (int, float)) or isinstance(attrs.get(target), bool):
                raise AstError(f'找不到临时属性: "{attribute}"')
            field_attrs[target] = _num(field_attrs.get(target)) + _num(value)
        return field_attrs

    # ---- 防御乘区 ----
    def level_reduce_rate(self, enemy_level: float) -> float:
        if enemy_level < 200:
            return 1.0
        return 1 / (1 + (enemy_level - 190) * 0.05)

    def defense_multiplier(self, merged: dict, is_skill: bool) -> float:
        """对齐 calculateDefenseMultiplier。"""
        enemy_level = _num(self.combat.get("enemyLevel")) or 80
        if _num(self.combat.get("enemyShield")) > 0:
            return self.level_reduce_rate(enemy_level)
        char_level = _num(self.combat.get("charLevel")) or 80
        level_diff = max(0.0, min(20.0, min(80.0, enemy_level) - char_level))
        ignore = _num(merged.get("技能无视防御")) + _num(merged.get("无视防御")) if is_skill else _num(merged.get("无视防御"))
        defense = _num(self.combat.get("enemyDef")) * (1 - ignore)
        reduce_rate = defense / (300 + defense - level_diff * 10)
        return max(0.0, min(1.0, (1 - reduce_rate) * self.level_reduce_rate(enemy_level)))

    def get_def(self, base: str | None, field_name: str | None, temp: dict | None, ctx_safe: str | None) -> float:
        """对齐 getDef：武器槽位 key 进防御（非技能），否则技能防御。"""
        key = base or self.base_name
        is_weapon = key in self.panels and self.panels.get(key) is not None
        merged = self.summon_attrs(key, temp, field_name, ctx_safe) if temp else self.attrs
        return self.defense_multiplier(merged, not is_weapon)

    # ---- 格式表达式 ----
    def evaluate_format(self, fmt: str, value1: float, value2: float, base_value: float, ev) -> float:
        """对齐 evaluateExpression：{%}→(v*base)、{}→v，×→*，失败回 value1*base。"""
        count = 0

        def _sub(match: re.Match) -> str:
            nonlocal count
            count += 1
            value = value1 if count % 2 == 1 else value2
            if match.group(0) == "{%}":
                return f"({value} * {base_value})"
            return str(value)

        expr = re.sub(r"\{%\}|\{\}", _sub, fmt).replace("×", "*")
        try:
            result = ev(parse_ast(expr), {}, {})
        except AstError:
            # 对齐 Function 兜底：只保留基本算术字符后重算，仍失败回 value1*base
            try:
                safe = re.sub(r"[^0-9+\-*/.()\s]", "", expr)
                result = ev(parse_ast(safe), {}, {}) if safe.strip() else float("nan")
            except AstError:
                return value1 * base_value
        if isinstance(result, float) and math.isnan(result):
            return value1 * base_value
        return result

    # ---- 技能值 ----
    def evaluate_skill(self, field_name: str, ns: str | None, temp: dict | None, ctx_safe: str | None, ev) -> float:
        """对齐 evaluateSkill：武器字段 / [攻击]/[防御]/[生命] / 伤害·治疗 / 普通字段。"""
        weapon_base = self.weapon_field_base(field_name)
        field_base = weapon_base or ns
        current = self.summon_attrs(field_base, temp, field_name, ctx_safe)
        if weapon_base:
            if weapon_base not in self.panels:
                return 0.0
            panel = self.temp_weapon_attr(weapon_base, field_name, temp, ctx_safe) or {}
            return (_num(current.get("攻击")) + _num(panel.get("攻击"))) * self.get_def(weapon_base, field_name, temp, ctx_safe)
        if field_name == "[攻击]":
            panel = self.temp_weapon_attr(ns, field_name, temp, ctx_safe) or {}
            return (_num(current.get("攻击")) + _num(panel.get("攻击"))) * self.get_def(ns, field_name, temp, ctx_safe)
        if field_name == "[防御]":
            return _num(current.get("防御")) * self.get_def(ns, field_name, temp, ctx_safe)
        if field_name == "[生命]":
            return _num(current.get("生命")) * self.get_def(ns, field_name, temp, ctx_safe)
        table = self._table(ctx_safe) if ctx_safe else (self.tables.get(ns or self.base_name) or [])
        field = self.find_field(table, field_name)
        if not field:
            return 0.0
        name = field.get("名称") or ""
        if name.endswith("伤害") or name.endswith("治疗"):
            value1 = _num(field.get("值"))
            value2 = _num(field.get("值2"))
            kind = field.get("基础")
            if not kind:
                patk = _num((self.temp_weapon_attr(ns, field_name, temp, ctx_safe) or {}).get("攻击"))
                base_value = _num(current.get("攻击")) + patk
            elif kind == "生命":
                base_value = _num(current.get("生命"))
            elif kind == "防御":
                base_value = _num(current.get("防御"))
            else:
                base_value = _num(current.get("攻击"))
            fmt = field.get("格式")
            if isinstance(fmt, str):
                # 传统值与格式串并存时 TS 取格式串路径（value1/value2 照传）
                base_damage = self.evaluate_format(fmt, value1, value2, base_value, ev)
            else:
                base_damage = value1 * base_value + value2
            if name.endswith("治疗"):
                return base_damage
            return base_damage * self.get_def(ns, field_name, temp, ctx_safe)
        return _num(field.get("值"))

    def is_damage_skill_field(self, field_name: str, base: str | None, ctx_safe: str | None) -> bool:
        """对齐 isDamageSkillField。"""
        if field_name in ("[攻击]", "[防御]", "[生命]") or self.weapon_field_base(field_name):
            return True
        table = self._table(ctx_safe) if ctx_safe else (self.tables.get(base or self.base_name) or [])
        field = self.find_field(table, field_name)
        if not field:
            return False
        name = field.get("名称") or ""
        return name.endswith("伤害") or name.endswith("伤害倍率")

    # ---- 伤害乘区 ----
    def _boost(self, merged: dict) -> float:
        hp = max(0.0, min(1.0, _num(self.combat.get("hpPercent", 1))))
        return 1.0 + _num(merged.get("昂扬")) * hp

    def _desperate(self, merged: dict) -> float:
        hp = max(0.25, min(1.0, _num(self.combat.get("hpPercent", 1))))
        return 1.0 + 4.0 * _num(merged.get("背水")) * (1.0 - hp) * (1.5 - hp)

    def _trigger_mult(self, dtype: str | None) -> float:
        """对齐 getTriggerMultiplier（武器路由）。"""
        resistance = _num(self.combat.get("resistance"))
        bonus = _num(self.combat.get("triggerBonus"))
        if dtype == "灾厄":
            return 1 + bonus if resistance != 0 else 0.0
        if dtype == (self.combat.get("hpTypeDMG") or {}).get(self.combat.get("currentHPType")):
            return _num((self.combat.get("hpTypeCoefficients") or {}).get(self.combat.get("currentHPType"))) + bonus
        return 0.0

    def _skill_damage(self, merged: dict, base: str | None, field_name: str | None, ctx_safe: str | None) -> dict:
        """对齐 calculateSkillDamage：返回乘区形态 DamageResult（不含面板基值）。"""
        table_safe = ctx_safe
        if not table_safe:
            for entry in self.skill_names:
                if entry.get("名称") == (base or self.base_name):
                    table_safe = entry.get("safeName")
                    break
        field = self.find_field_wide(self._table(table_safe), field_name or "") if field_name else None
        is_summon = bool(field and "召唤物" in (field.get("tag") or []))
        di_base = 1 + _num(merged.get("增伤")) + _num(merged.get("技能伤害")) + (_num(merged.get("召唤物伤害")) if is_summon else 0)
        elem_inc = _num(merged.get("元素增伤"))
        phys_inc = _num(merged.get("物理增伤"))
        other = (1 + _num(merged.get("独立增伤"))) * ((1 + _num(merged.get("召唤物独立增伤"))) if is_summon else 1)
        other *= (_num(merged.get("失衡易伤")) + 1.5) if self.combat.get("imbalance") else 1
        other *= max(0.0, 1 + _num(merged.get("属性穿透")))
        hp_more = self._boost(merged) * self._desperate(merged)
        resistance = _num(self.combat.get("resistance"))
        factor = lambda r: max(0.0, 1 - r)
        flipped = max(0.0, 1 - (-4 if resistance > 0 else 0.5))
        active = "转属克" if resistance > 0 else ("转属逆" if resistance < 0 else None)
        raw_elem = max(0.0, _num(merged.get(active))) if active else 0.0
        raw_phys = sum(max(0.0, _num(merged.get(k))) for k in PHYSICAL_CONVERTS)
        pool = raw_elem + raw_phys
        scale = 1 / pool if pool > 1 else 1.0
        rest = max(0.0, 1 - min(1.0, pool))
        parts = []
        if rest > 0:
            parts.append((rest, factor(resistance), True))
        if active:
            ratio = max(0.0, _num(merged.get(active))) * scale
            if ratio > 0:
                parts.append((ratio, flipped, True))
        for key in PHYSICAL_CONVERTS:
            ratio = max(0.0, _num(merged.get(key))) * scale
            if ratio > 0:
                parts.append((ratio, 1.0, False))
        phys = sum(r * f * (di_base + (elem_inc if el else phys_inc)) for r, f, el in parts if not el)
        elem = sum(r * f * (di_base + (elem_inc if el else phys_inc)) for r, f, el in parts if el)
        total = phys + elem
        return {
            "expectedDamage": total * other * hp_more,
            "noHpDamage": total * other,
            "physicalDamage": phys * other * hp_more,
            "elementDamage": elem * other * hp_more,
        }

    def _weapon_damage(self, merged: dict, key: str, field_damage_type: str | None) -> dict:
        """对齐 calculateWeaponDamage：返回乘区形态 DamageResult。"""
        w = self.weapons.get(key) or {}
        weapon_attrs = merged.get("weapon") or {}
        total = _num(merged.get("攻击")) + _num(weapon_attrs.get("攻击"))
        inherit_all = bool(w.get("isSkillWeapon")) and bool(w.get("inherit")) and w.get("atk") == "all"
        dtype = field_damage_type or w.get("伤害类型")
        convert_phys = dtype == "灾厄" and not inherit_all
        if convert_phys:
            phys_share, elem_share = 1.0, 0.0
        elif inherit_all:
            phys_share, elem_share = 0.0, 1.0
        else:
            phys_share = _num(weapon_attrs.get("攻击")) / total if total else 0.0
            elem_share = _num(merged.get("攻击")) / total if total else 0.0
        trigger_rate = min(1.0, max(0.0, _num(weapon_attrs.get("触发"))))
        crit_rate = _num(weapon_attrs.get("暴击"))
        crit_dmg = _num(weapon_attrs.get("暴伤"))
        lower_cd = (crit_dmg - 1) * math.floor(crit_rate) + 1
        higher_cd = (crit_dmg - 1) * math.ceil(crit_rate) + 1
        crit_exp = 1 + crit_rate * (crit_dmg - 1)
        resistance = _num(self.combat.get("resistance"))
        flipped = max(0.0, 1 - (-4 if resistance > 0 else 0.5))
        pen = max(0.0, 1 + _num(merged.get("属性穿透")))
        hp_more = self._boost(merged) * self._desperate(merged)
        di_base = 1 + _num(merged.get("增伤")) + _num(weapon_attrs.get("增伤")) + _num(merged.get("武器伤害"))
        elem_inc = _num(merged.get("元素增伤"))
        phys_inc = _num(merged.get("物理增伤"))
        other = (1 + _num(merged.get("独立增伤"))) * (1 + _num(weapon_attrs.get("独立增伤")))
        other *= 1 + _num(weapon_attrs.get("追加伤害"))
        other *= (_num(merged.get("失衡易伤")) + 1.5) if self.combat.get("imbalance") else 1
        other *= pen
        common = hp_more * other
        active = "转属克" if resistance > 0 else ("转属逆" if resistance < 0 else None)
        raw_elem = max(0.0, _num(merged.get(active))) if active else 0.0
        raw_phys = sum(max(0.0, _num(merged.get(k))) for k in PHYSICAL_CONVERTS)
        pool = raw_elem + raw_phys
        scale = 1 / pool if pool > 1 else 1.0
        rest = max(0.0, 1 - min(1.0, pool))
        elem_ratio = min(1.0, raw_elem) * scale
        f = lambda r: max(0.0, 1 - r)
        if resistance == 0:
            res_factor = f(0)
        elif resistance < 0:
            res_factor = (1 - elem_ratio) * f(resistance) + elem_ratio * f(0.5)
        else:
            res_factor = (1 - elem_ratio) * f(resistance) + elem_ratio * f(-4)
        entries = []
        if active:
            entries.append((raw_elem, "element", active))
        for k in PHYSICAL_CONVERTS:
            entries.append((max(0.0, _num(merged.get(k))), "physical", k))
        elem_part = elem_share * res_factor
        parts = []
        if inherit_all:
            if rest > 0:
                parts.append((elem_part * rest, 0.0, True))
        else:
            if phys_share > 0 and rest > 0:
                parts.append((phys_share * rest, self._trigger_mult(dtype), False))
            if rest > 0:
                parts.append((elem_part * rest, 0.0, True))
        for ratio, kind, conv_key in entries:
            ratio *= scale
            if ratio <= 0:
                continue
            if kind == "element":
                if phys_share > 0:
                    parts.append((phys_share * ratio * flipped, 0.0, True))
                parts.append((elem_part * ratio, 0.0, True))
            else:
                trig = self._conv_trigger(conv_key)
                if inherit_all:
                    parts.append((elem_part * ratio, trig, False))
                else:
                    if phys_share > 0:
                        parts.append((phys_share * ratio, trig, False))
                    parts.append((elem_part * ratio, trig, False))
        inc = lambda el: di_base + (elem_inc if el else phys_inc)
        all_part = sum(r * inc(el) for r, _, el in parts)
        trig_part = sum(r * (1 + t) * inc(el) for r, t, el in parts)
        exp_part = sum(r * (1 + t * trigger_rate) * inc(el) for r, t, el in parts)
        phys_exp = sum(r * (1 + t * trigger_rate) * inc(el) for r, t, el in parts if not el)
        elem_exp = sum(r * (1 + t * trigger_rate) * inc(el) for r, t, el in parts if el)
        exp_crit_base = crit_exp * common
        return {
            "lowerCritNoTrigger": all_part * (lower_cd * common),
            "higherCritNoTrigger": all_part * (higher_cd * common),
            "lowerCritTrigger": trig_part * (lower_cd * common),
            "higherCritTrigger": trig_part * (higher_cd * common),
            "lowerCritExpectedTrigger": exp_part * (lower_cd * common),
            "higherCritExpectedTrigger": exp_part * (higher_cd * common),
            "expectedCritTrigger": exp_part * exp_crit_base,
            "expectedCritNoTrigger": all_part * exp_crit_base,
            "expectedDamage": exp_part * exp_crit_base,
            "noHpDamage": exp_part * crit_exp * other,
            "physicalDamage": phys_exp * exp_crit_base,
            "elementDamage": elem_exp * exp_crit_base,
        }

    def _conv_trigger(self, key: str) -> float:
        return self._trigger_mult({"转切割": "切割", "转贯穿": "贯穿", "转震荡": "震荡", "转灾厄": "灾厄"}.get(key, key))

    def get_damage(self, base: str | None, field_name: str | None, temp: dict | None, ctx_safe: str | None) -> dict:
        """对齐 getDamage（含充盈乘区与攻击细分加成日志表）。"""
        weapon_base = self.weapon_field_base(field_name)
        key = weapon_base or base or self.base_name
        cache_key = (key, field_name, ctx_safe)
        if not temp and cache_key in self._damage_cache:
            return self._damage_cache[cache_key]
        merged = self.summon_attrs(key, temp, field_name, ctx_safe)
        table = self._table(ctx_safe) if ctx_safe else (self.tables.get(key) or [])
        field = self.find_field_wide(table, field_name or "") if field_name else None
        field_dtype = (field or {}).get("伤害类型")
        w = self.weapons.get(key) if isinstance(self.weapons.get(key), dict) else None
        arrow = bool(
            w and field_dtype == "灾厄" and w.get("类型") == "远程" and self.combat.get("hasArrowRainMod")
        )
        weapon_attr = self.temp_weapon_attr(key, field_name, temp, ctx_safe)
        if w is not None and weapon_attr:
            tags = None
            if isinstance(field, dict):
                tags = field.get("tag")
            if self.attack_bonus_fn is not None:
                bonus_inc = _num(self.attack_bonus_fn(key, field_name, "增伤", (w or {}).get("类型"), tags, ctx_safe))
                bonus_ind = _num(self.attack_bonus_fn(key, field_name, "独立增伤", (w or {}).get("类型"), tags, ctx_safe))
            else:
                bonus_inc = _num((self.attack_bonus.get(f"{key}::{field_name}::增伤", 0)))
                bonus_ind = _num((self.attack_bonus.get(f"{key}::{field_name}::独立增伤", 0)))
            indep = (1 + _num(weapon_attr.get("独立增伤"))) / (1 + (-0.6 if arrow else 0))
            merged = {
                **merged,
                "weapon": {
                    **weapon_attr,
                    "增伤": _num(weapon_attr.get("增伤")) + bonus_inc,
                    "独立增伤": indep * (1 + bonus_ind) - 1,
                },
            }
            damage = self._weapon_damage(merged, key, field_dtype)
        else:
            if self.attack_bonus_fn is not None:
                weapon = self.weapons.get(key) if isinstance(self.weapons.get(key), dict) else None
                bonus_inc = _num(self.attack_bonus_fn(key, field_name, "增伤", (weapon or {}).get("类型"), None, ctx_safe))
                bonus_ind = _num(self.attack_bonus_fn(key, field_name, "独立增伤", (weapon or {}).get("类型"), None, ctx_safe))
            else:
                bonus_inc = _num((self.attack_bonus.get(f"{key}::{field_name}::增伤", 0)))
                bonus_ind = _num((self.attack_bonus.get(f"{key}::{field_name}::独立增伤", 0)))
            merged = {
                **merged,
                "增伤": _num(merged.get("增伤")) + bonus_inc,
                "独立增伤": (1 + _num(merged.get("独立增伤"))) * (1 + bonus_ind) - 1,
            }
            damage = self._skill_damage(merged, base, field_name, ctx_safe)
        fullness_mult = 1.0
        if field and "充盈" in (field.get("tag") or []):
            fullness_mult = 1 + _num(merged.get("充盈威力"))
        else:
            conv = max(0.0, _num(merged.get("转充盈")))
            if conv > 0:
                fullness_mult = 1 + conv * _num(merged.get("充盈威力"))
        if fullness_mult != 1:
            damage = {k: (v * fullness_mult if isinstance(v, (int, float)) else v) for k, v in damage.items()}
        if not temp:
            self._damage_cache[cache_key] = damage
        return damage

    def damage_member(self, damage: dict, member: str | None) -> float:
        """对齐 evaluateMember（无成员即期望；未知成员 0）。"""
        if not member:
            return _num(damage.get("expectedDamage"))
        name = re.sub(r"[非低]", "未", member)
        table = {
            "N": "noHpDamage",
            "物理": "physicalDamage",
            "元素": "elementDamage",
            "暴击": "higherCritExpectedTrigger",
            "未暴击": "lowerCritExpectedTrigger",
            "触发": "expectedCritTrigger",
            "未触发": "expectedCritNoTrigger",
            "暴击触发": "higherCritTrigger",
            "触发暴击": "higherCritTrigger",
            "未触发暴击": "higherCritNoTrigger",
            "暴击未触发": "higherCritNoTrigger",
            "触发未暴击": "lowerCritTrigger",
            "未暴击触发": "lowerCritTrigger",
            "未暴击未触发": "lowerCritNoTrigger",
            "未触发未暴击": "lowerCritNoTrigger",
        }
        key = table.get(name)
        if key is None:
            return 0.0
        value = damage.get(key)
        return _num(value) if value is not None else _num(damage.get("expectedDamage"))
