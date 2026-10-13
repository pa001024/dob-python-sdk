"""特效 code 执行 + 求索折算回归（对齐 TS src/data/tests/effect-code.test.ts）。

合成最小 state/上下文，不依赖 mock 数据包版本（旧包无 10199 数据）。
"""
import unittest

from dna_builder_sdk.calc.damage import DamageContext
from dna_builder_sdk.calc.engine import Engine


def _char():
    return {
        "id": 1, "名称": "测", "属性": "雷", "精通": ["单手剑"],
        "基础攻击": 100.0, "基础生命": 1000.0, "基础护盾": 500.0,
        "基础防御": 50.0, "基础神智": 100.0,
    }


def _state(char_mods=None, weapons=None, buffs=None):
    melee = (weapons or {}).get("melee") or {}
    ranged = (weapons or {}).get("ranged") or {}
    return {
        "char": _char(),
        "charMods": char_mods or [], "meleeMods": [], "rangedMods": [], "skillMods": [],
        "meleeWeapon": melee, "rangedWeapon": ranged,
        "buffs": buffs or [], "dynamicBuffs": [b for b in (buffs or []) if b.get("code")],
        "allSkills": [],
        "resonanceGain": 0.0, "hpPercent": 1.0,
        "enemy": {"def": 0, "_等级": 80, "currentShield": 0},
        "enemyId": 130, "enemyLevel": 80, "enemyResistance": 0.0,
    }


def _code_mod(lv):
    return {
        "id": 999001, "名称": "码效测试", "系列": "测试", "品质": "金",
        "耐受": 0, "类型": "角色", "_等级": 10,
        "buff": {"名称": "码效特效", "mx": 1, "_等级": lv, "code": "增伤+=0.5"},
    }


class EffectCodeTest(unittest.TestCase):
    def test_mod_effect_code_executes(self):
        on = Engine(_state([_code_mod(1)]), None).calculate_attributes()
        off = Engine(_state([_code_mod(0)]), None).calculate_attributes()
        self.assertAlmostEqual(on["增伤"] - off["增伤"], 0.5, places=9)

    def test_weapon_effect_code_forge_gate(self):
        def weapon(forge):
            return {
                "id": 10199, "名称": "无解谜题", "类型": "近战", "forgeEffective": forge,
                "buff": {"名称": "无解谜题", "mx": 6, "_等级": 6, "code": "增伤+=0.5"},
            }

        on = Engine(_state([], {"melee": weapon(True)}), None).calculate_attributes()
        off = Engine(_state([], {"melee": weapon(False)}), None).calculate_attributes()
        self.assertAlmostEqual(on["增伤"] - off["增伤"], 0.5, places=9)

    def test_scoped_code_with_attr_terminates(self):
        """技能限定 code + attr 型 BUFF 共存时基线重入必须终止（栈溢出回归）。"""
        buffs = [
            {"名称": "黎瑟·浴火2溯", "技能": "伤害", "code": "if(技能速度>2)独立增伤=(1+独立增伤)*(1+技能速度*0.15)-1"},
            {"名称": "无解谜题5熔", "技能": "求索", "attr": {"技能倍率乘数": "技能速度-1"}},
        ]
        attrs = Engine(_state([], None, buffs), None).calculate_attributes()
        self.assertTrue(all(
            v == v and abs(v) != float("inf")
            for v in attrs.values() if isinstance(v, (int, float)) and not isinstance(v, bool)
        ))


def _qiusuo_ctx(skill_trigger, weapon_trigger):
    field = {
        "名称": "[求索]伤害", "影响": "技能威力", "伤害类型": "灾厄", "值": 35.0,
        "tag": ["技能", "近战", "武器"],
    }
    weapon = {"名称": "无解谜题", "类型": "近战", "伤害类型": "灾厄"}
    panel = {
        "攻击": 100.0, "暴击": 0.3, "暴伤": 2.0, "触发": weapon_trigger, "攻速": 1.0, "多重": 1.0,
        "增伤": 0.0, "独立增伤": 0.0, "追加伤害": 0.0, "装填": 1.0, "弹匣": 20.0, "弹药": 100.0,
        "武器倍率": 0.0, "充盈转化": 1.0, "召唤物攻击速度转化": 0.0, "召唤物范围转化": 0.0,
    }
    attrs = {
        "攻击": 1000.0, "增伤": 0.0, "技能伤害": 0.0, "元素增伤": 0.0, "物理增伤": 0.0,
        "独立增伤": 0.0, "技能追伤": 0.0, "技能无视防御": 0.0, "属性穿透": 0.0,
        "技能威力": 1.0, "技能触发": skill_trigger, "充盈威力": 0.0, "转充盈": 0.0,
    }
    return DamageContext(
        attrs=attrs,
        panels={"求索": panel, "近战": panel},
        skill_tables={"求索": [field]},
        skill_names=[], aliases={}, rules=[], attack_bonus={},
        weapons={"求索": weapon}, weapon_bases={},
        combat={"resistance": 0.5}, base_name="普通攻击",
    )


class QiusuoTriggerTest(unittest.TestCase):
    def _dmg(self, st, wt):
        return _qiusuo_ctx(st, wt).get_damage("求索", "[求索]伤害", {}, "")

    def test_coverage_ratio(self):
        # 触发期望按 bonus 模型：(1 + 0.74) / (1 + 0.29)（灾厄 bonus = 1，抗性存在）
        hi = self._dmg(0.54, 0.2)["expectedDamage"]
        lo = self._dmg(0.09, 0.2)["expectedDamage"]
        self.assertGreater(lo, 0)
        self.assertAlmostEqual(hi / lo, 1.74 / 1.29, places=5)

    def test_no_skill_trigger_falls_back_to_weapon_trigger(self):
        # bonus 模型：无技能触发时按 (1 + 武器面板触发) 结算，封顶对照为全额 2 倍
        floor = self._dmg(0, 0.2)["expectedDamage"]
        capped = self._dmg(1, 0.2)["expectedDamage"]
        self.assertAlmostEqual(floor / capped, 1.2 / 2.0, places=5)

    def test_weapon_trigger_ignored_when_capped(self):
        a = self._dmg(1, 0.2)["expectedDamage"]
        b = self._dmg(1, 2.0)["expectedDamage"]
        self.assertAlmostEqual(b / a, 1.0, places=6)

    def test_weapon_trigger_counts_when_uncapped(self):
        c = self._dmg(0, 0.2)["expectedDamage"]
        d = self._dmg(0, 0.5)["expectedDamage"]
        self.assertAlmostEqual(d / c, 1.5 / 1.2, places=5)

    def test_damage_pools(self):
        """求索伤害视为技能伤害：吃技能伤害，不吃武器伤害/武器追伤。"""
        base = self._dmg(1, 0.2)["expectedDamage"]
        self.assertGreater(base, 0)

        def with_attrs(**kw):
            ctx = _qiusuo_ctx(1, 0.2)
            ctx.attrs.update(kw)
            return ctx.get_damage("求索", "[求索]伤害", {}, "")["expectedDamage"]

        self.assertAlmostEqual(with_attrs(技能伤害=1.0) / base, 2.0, places=5)
        self.assertAlmostEqual(with_attrs(武器伤害=1.0) / base, 1.0, places=6)

        def with_panel(**kw):
            ctx = _qiusuo_ctx(1, 0.2)
            ctx.panels["求索"].update(kw)
            return ctx.get_damage("求索", "[求索]伤害", {}, "")["expectedDamage"]

        self.assertAlmostEqual(with_panel(追加伤害=1.0) / base, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()


class CalamityConvertNoWeaponTest(unittest.TestCase):
    def _dmg(self, skill_trigger):
        ctx = DamageContext(
            attrs={"技能触发": skill_trigger},
            panels={}, skill_tables={}, skill_names=[],
            aliases={}, rules=[], attack_bonus={}, weapons={}, weapon_bases={},
            combat={"resistance": -4}, base_name="普通攻击",
        )
        merged = {
            "增伤": 0.0, "技能伤害": 0.0, "元素增伤": 0.0, "物理增伤": 0.0,
            "独立增伤": 0.0, "技能追伤": 0.0, "属性穿透": 0.0, "失衡易伤": 0.0,
            "昂扬": 0.0, "背水": 0.0, "技能触发": skill_trigger, "转灾厄": 1.0,
        }
        return ctx._damage(merged, weapon=None, panel=None, is_skill_pool=True)["expectedDamage"]

    def test_no_weapon_calamity_convert_triggers(self):
        # 无武器归属的转灾厄依然结算灾厄触发（只需敌方存在抗性），按技能触发率
        self.assertAlmostEqual(self._dmg(1.0) / self._dmg(0.0), 2.0, places=6)
