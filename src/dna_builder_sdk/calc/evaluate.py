"""纯表达式求值：与 TS 侧 CharBuild.evaluateAST 在纯属性模式下逐项对齐。

覆盖范围（无编辑语义）：
- number / binary(+ - * / % //，除零得 0，// 为 floor 除法）/ unary(+ -)
- property：scope > 自定义变量（递归，环引用得 0）> 临时属性叠加 > 面板链；
  ns 路径复刻 TS falsy 链 `weapon(ns) → attr`（skill 段缺上下文恒 0），bare 路径为
  `attr → attrs.weapon`；`!`（forceAttr）跳过 scope 与自定义变量
- 内置函数：min / max / floor / ceil / or（首个非零，否则末项）/ log / power / hp
- 自定义函数：形参绑定 scope 求值函数体，递归调用得 0
- member_access：plain-attrs 模式——attrs[基名] 为 dict 且含成员则取之；
  已知伤害分支名回退基值（对齐 TS 的 `|| expectedDamage` 兜底）；未知成员得 0
- temporary_attributes：同名属性按增量累加后求值目标
- weapon_panels：`ns → WeaponAttr` 面板（oracle 导出时已按 selectedWeapon 消解）；
  技能字段读取不在本 SDK（需完整构筑上下文，恒 0 并计入 parity 豁免）

超出范围（需完整构筑上下文）：技能字段（含伤害/治疗结算）、DOT 分量。
"""

from __future__ import annotations

import math

from .ast import AstError, parse_ast

# TS 侧已知的伤害分支成员名（evaluateMember）：命中时回退期望值
_DAMAGE_BRANCH_MEMBERS = frozenset(
    [
        "N",
        "物理",
        "元素",
        "暴击",
        "未暴击",
        "触发",
        "未触发",
        "暴击触发",
        "触发暴击",
        "未触发暴击",
        "暴击未触发",
        "触发未暴击",
        "未暴击触发",
        "未暴击未触发",
        "未触发未暴击",
    ]
)


def _num(value) -> float:
    """宽松转数值：数字与数字字符串互认，失败得 0。"""
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


def _boost_multiplier(attrs: dict, hp_percent: float) -> float:
    """昂扬乘区：1 + 昂扬 * clamp(hp, 0, 1)，对齐 calculateBoostMultiplier。"""
    hp = max(0.0, min(1.0, _num(hp_percent)))
    return 1.0 + _num(attrs.get("昂扬", 0.0)) * hp


def _desperate_multiplier(attrs: dict, hp_percent: float) -> float:
    """背水乘区：1 + 4*背水*(1-hp)*(1.5-hp)，hp 下限 0.25，对齐 TS。"""
    hp = max(0.25, min(1.0, _num(hp_percent)))
    return 1.0 + 4.0 * _num(attrs.get("背水", 0.0)) * (1.0 - hp) * (1.5 - hp)


def _lookup_attr(attrs: dict, name: str, namespace: str | None, weapon_panels: dict | None = None) -> float:
    """属性取值：TS falsy 链的无技能复刻（技能段恒 0，见模块 docstring）。

    ns 路径：weapon(ns) → `ns::name` → attr(bare)；bare 路径：attr → attrs.weapon。
    链上 0/缺失继续下探（对齐 JS `||`），dict 值不直接取值。
    """
    if namespace:
        panel = (weapon_panels or {}).get(namespace)
        if isinstance(panel, dict) and name in panel and not isinstance(panel[name], dict):
            value = _num(panel[name])
            if value:
                return value
        key = f"{namespace}::{name}"
        if key in attrs and not isinstance(attrs[key], dict):
            value = _num(attrs[key])
            if value:
                return value
        if name in attrs and not isinstance(attrs[name], dict):
            return _num(attrs[name])
        return 0.0
    if name in attrs and not isinstance(attrs[name], dict):
        value = _num(attrs[name])
        if value:
            return value
    selected = attrs.get("weapon")
    if isinstance(selected, dict) and name in selected and not isinstance(selected[name], dict):
        return _num(selected[name])
    return 0.0


def evaluate(
    expr,
    attrs: dict | None = None,
    scope: dict | None = None,
    custom_variables: dict[str, str] | None = None,
    custom_functions: dict | None = None,
    weapon_panels: dict | None = None,
    damage_ctx=None,
    _temporary: dict | None = None,
    _resolving_vars: frozenset | None = None,
    _resolving_funcs: frozenset | None = None,
    _ast_cache: dict | None = None,
) -> float:
    """求值表达式（字符串或 parse_ast 节点），返回 float。

    :param expr: 表达式字符串或 AST dict
    :param attrs: 属性表（名称 → 数值；dict 值仅供成员访问）
    :param scope: 自定义函数形参绑定
    :param custom_variables: 名称 → 表达式（只读求值，不提供编辑语义）
    :param custom_functions: 名称 → ([形参], 函数体表达式）或 {"params","expression"} dict
    :param weapon_panels: `ns → WeaponAttr` 面板（evaluate 内 ns 解析用）
    :param damage_ctx: calc.damage.DamageContext（技能伤害结算链；缺省为纯属性模式）
    """
    attributes = attrs or {}
    node = parse_ast(expr) if isinstance(expr, str) else expr
    resolving_vars = _resolving_vars or frozenset()
    resolving_funcs = _resolving_funcs or frozenset()
    cache: dict = _ast_cache if _ast_cache is not None else {}
    current_scope = scope or {}
    temporary = _temporary or {}

    def ev(n, overlay: dict, local_scope: dict) -> float:
        kind = n["type"]
        if kind == "number":
            return float(n["value"])
        if kind == "binary":
            left = ev(n["left"], overlay, local_scope)
            right = ev(n["right"], overlay, local_scope)
            op = n["operator"]
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                return left / right if right != 0 else 0.0
            if op == "%":
                return left % right if right != 0 else 0.0
            if op == "//":
                return float(math.floor(left / right)) if right != 0 else 0.0
            raise AstError(f"未知的二元运算符: {op}")
        if kind == "unary":
            arg = ev(n["argument"], overlay, local_scope)
            if n["operator"] == "+":
                return +arg
            if n["operator"] == "-":
                return -arg
            raise AstError(f"未知的一元运算符: {n['operator']}")
        if kind == "property":
            return ev_property(n, overlay, local_scope)
        if kind == "function":
            args = [ev(a, overlay, local_scope) for a in n["args"]]
            return ev_function(n["name"], args, overlay, local_scope)
        if kind == "member_access":
            return ev_member(n, overlay, local_scope)
        if kind == "temporary_attributes":
            merged = dict(overlay)
            for item in n["attributes"]:
                merged[item["name"]] = merged.get(item["name"], 0.0) + ev(item["value"], overlay, local_scope)
            return ev(n["target"], merged, local_scope)
        raise AstError(f"未知的 AST 节点: {kind}")

    def ev_cb(nd, ov=None, sc=None):
        return ev(nd, ov if ov is not None else {}, sc if sc is not None else {})

    def _ctx_safe_of(n) -> str | None:
        ns = n.get("namespace")
        return damage_ctx.resolve_skill_safe(ns) if (damage_ctx and ns) else None

    def _merge_temp(node_attrs: list, inherited: dict, local_scope: dict) -> dict:
        """对齐 mergeTemporaryAttributes：各值按继承态求值后累加。"""
        merged = dict(inherited or {})
        for item in node_attrs:
            merged[item["name"]] = (merged.get(item["name"], 0.0) or 0.0) + ev(item["value"], inherited or {}, local_scope)
        return merged

    def _prop_context(node, inherited: dict, local_scope: dict):
        """对齐 getPropertyContext：解开 temporary 修饰链，返回 (字段节点, 合并后临时属性)。"""
        if node["type"] == "property":
            return (node, inherited)
        if node["type"] != "temporary_attributes":
            return None
        return _prop_context(node["target"], _merge_temp(node["attributes"], inherited, local_scope), local_scope)

    def ev_identity(n, overlay: dict, local_scope: dict) -> float:
        name, ns = n["name"], n.get("namespace")
        force = bool(n.get("forceAttr"))
        if not force and not ns and name in local_scope:
            return _num(local_scope[name])
        if not force and not ns and custom_variables and name in custom_variables:
            if name in resolving_vars:
                return 0.0
            body = custom_variables[name]
            key = f"var:{body}"
            sub = cache.get(key)
            if sub is None:
                sub = parse_ast(body)
                cache[key] = sub
            value = evaluate(
                sub,
                attributes,
                local_scope,
                custom_variables,
                custom_functions,
                weapon_panels,
                damage_ctx,
                overlay,
                resolving_vars | {name},
                resolving_funcs,
                cache,
            )
            return value if math.isfinite(value) else 0.0
        ctx_safe = _ctx_safe_of(n)
        if damage_ctx:
            # 对齐 evaluateIdentity：skill → weapon(ns)/attr → weapon(selected)/attr 的 falsy 链
            if not force:
                sval = damage_ctx.evaluate_skill(name, ns, overlay or None, ctx_safe, ev_cb)
                if sval:
                    return sval
            merged = damage_ctx.summon_attrs(ns, overlay or None, name, ctx_safe)
            if ns:
                panel = (weapon_panels or {}).get(ns)
                if isinstance(panel, dict) and name in panel:
                    value = _num(panel[name])
                    if value:
                        return value
                if name in merged and isinstance(merged.get(name), (int, float)) and not isinstance(merged.get(name), bool):
                    return _num(merged[name])
                return 0.0
            if name in merged and isinstance(merged.get(name), (int, float)) and not isinstance(merged.get(name), bool):
                value = _num(merged[name])
                if value:
                    return value
            selected = attributes.get("weapon")
            if isinstance(selected, dict) and name in selected and isinstance(selected[name], (int, float)):
                return _num(selected[name])
            return 0.0
        if overlay and name in overlay:
            # 无 ctx 时的近似：临时属性按增量叠加（TS 经 summon 合并，此处仅做平值加算）
            raw = attributes.get(name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return _num(raw) + _num(overlay[name])
            if name not in attributes:
                return _num(overlay[name])
        return _lookup_attr(attributes, name, ns, weapon_panels)

    def ev_property(n, overlay: dict, local_scope: dict) -> float:
        name, ns = n["name"], n.get("namespace")
        force = bool(n.get("forceAttr"))
        if not ns and not force and name in local_scope:
            return _num(local_scope[name])
        value = ev_identity(n, overlay, local_scope)
        if damage_ctx and not force:
            if not ns and custom_variables and name in custom_variables:
                return value
            sval = damage_ctx.evaluate_skill(name, ns, overlay or None, _ctx_safe_of(n), ev_cb)
            if sval and damage_ctx.is_damage_skill_field(name, ns, _ctx_safe_of(n)):
                damage = damage_ctx.get_damage(ns, name, overlay or None, _ctx_safe_of(n))
                return value * _num(damage.get("expectedDamage"))
        return value

    def ev_function(name: str, args: list[float], overlay: dict, local_scope: dict) -> float:
        if name == "min":
            return min(args)
        if name == "max":
            return max(args)
        if name == "floor":
            return float(math.floor(args[0]))
        if name == "ceil":
            return float(math.ceil(args[0]))
        if name == "or":
            for v in args:
                if v != 0:
                    return v
            return args[-1] if args else 0.0
        if name == "log":
            return math.log(args[0])
        if name == "power":
            return args[0] ** args[1]
        if name == "hp":
            hp = args[0] if args else 1.0
            return _desperate_multiplier(attributes, hp) * _boost_multiplier(attributes, hp)
        if custom_functions and name in custom_functions:
            if name in resolving_funcs:
                return 0.0
            definition = custom_functions[name]
            params, body = (definition if isinstance(definition, tuple) else (definition["params"], definition["expression"]))
            if len(params) != len(args):
                raise AstError(f'函数 "{name}" 需要 {len(params)} 个参数,实际传入 {len(args)} 个')
            key = f"func:{body}"
            sub = cache.get(key)
            if sub is None:
                sub = parse_ast(body)
                cache[key] = sub
            next_scope = {**local_scope, **dict(zip(params, args))}
            return evaluate(
                sub,
                attributes,
                next_scope,
                custom_variables,
                custom_functions,
                weapon_panels,
                damage_ctx,
                overlay,
                resolving_vars,
                resolving_funcs | {name},
                cache,
            )
        raise AstError(f"未知的函数: {name}")

    def ev_member(n, overlay: dict, local_scope: dict) -> float:
        obj, member = n["object"], n["property"]
        if damage_ctx:
            # 对齐 member_access：字段对象走 identity（不乘默认伤害系数），伤害字段再乘分支
            pc = _prop_context(obj, overlay, local_scope)
            if pc is not None:
                prop, merged_temp = pc
                object_value = ev_identity(prop, merged_temp or {}, local_scope)
                ctx_safe = _ctx_safe_of(prop)
                if prop.get("forceAttr") or not damage_ctx.is_damage_skill_field(prop["name"], prop.get("namespace"), ctx_safe):
                    return object_value
                damage = damage_ctx.get_damage(prop.get("namespace"), prop["name"], merged_temp or None, ctx_safe)
                return object_value * damage_ctx.damage_member(damage, member)
            object_value = ev(obj, overlay, local_scope)
            damage = damage_ctx.get_damage(None, None, overlay or None, None)
            return object_value * damage_ctx.damage_member(damage, member)
        return _legacy_member_value(n, overlay, local_scope)

    def _legacy_member_value(n, overlay: dict, local_scope: dict) -> float:
        obj, member = n["object"], n["property"]
        base = 0.0
        container = None
        if obj["type"] == "property":
            base = ev_identity(obj, overlay, local_scope)
            key = obj["name"] if not obj.get("namespace") else f"{obj['namespace']}::{obj['name']}"
            raw = overlay.get(key, attributes.get(key, attributes.get(obj["name"])))
            if isinstance(raw, dict):
                container = raw
        else:
            base = ev(obj, overlay, local_scope)
            if isinstance(base, dict):
                container = base
                base = 0.0
        if container is not None and member in container:
            return _num(container[member])
        if member in _DAMAGE_BRANCH_MEMBERS:
            return base
        return 0.0

    result = ev(node, dict(temporary), dict(current_scope))
    return result if math.isfinite(result) else 0.0
