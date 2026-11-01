"""构筑聚合引擎（wasm 后端唯一实现）。

纯 BD JSON（CharSettings + charId）进，目标值出：装配、属性汇总、面板、
技能结算全部走内嵌 `dob-calc-core` wasm（见 `calc/wasm.py`），与真机逐位一致。
`state` 为 `build.build_state` 装配的轻量 dict（仅 id/设置/技能等级），
不含任何实体等级化数据。
"""

from __future__ import annotations

import math

from .wasm import WasmBackend, WasmEvaluateError


def inline_panels(result: dict | None) -> dict | None:
    """evaluate 信封内联 `panels`（上游新增的面板计算 API）。

    非空 dict 时直接采用，免去一次 `dobcc_panels` 往返；缺失/空/非法时返回 None，
    调用方回退专用 panels 端点（行为与之前逐位一致）。"""
    if not isinstance(result, dict):
        return None
    panels = result.get("panels")
    if isinstance(panels, dict) and panels:
        return {k: dict(v) if isinstance(v, dict) else v for k, v in panels.items()}
    return None


class Engine:
    """一次构筑的聚合状态机（state 为 build.py 装配的轻量 dict）。"""

    def __init__(self, state: dict, tables):
        self.s = state
        self.t = tables
        self._wasm = None
        self._wasm_attrs = None
        self._wasm_panels = None

    # ---------- wasm 会话 ----------

    def _wasm_backend(self) -> WasmBackend:
        """惰性 wasm 会话（首次热调用创建）。"""
        if self._wasm is None:
            self._wasm = WasmBackend(self.t)
        return self._wasm

    def _wasm_full_attrs(self) -> dict:
        """wasm 全量属性（attributes + weapon，缺失/NaN 归 0），按构筑缓存。"""
        if self._wasm_attrs is None:
            result = self._wasm_backend().evaluate(
                self.s.get("charId"), self.s.get("settings") or {}
            )
            attrs = {k: (0.0 if v is None else v) for k, v in (result.get("attributes") or {}).items()}
            if result.get("weapon"):
                attrs["weapon"] = {k: (0.0 if v is None else v) for k, v in result["weapon"].items()}
            self._wasm_attrs = attrs
            inlined = inline_panels(result)
            if inlined is not None:
                self._wasm_panels = inlined
        return self._wasm_attrs

    def _wasm_all_panels(self) -> dict:
        """全槽武器面板（键集与上游 `weapon_panels` 一致），按构筑缓存。"""
        if self._wasm_panels is None:
            self._wasm_panels = self._wasm_backend().panels(
                self.s.get("charId"), self.s.get("settings") or {}
            )
        return self._wasm_panels

    @staticmethod
    def _copy_panel(panel) -> dict | None:
        return dict(panel) if isinstance(panel, dict) else None

    # ---------- 求值 ----------

    def calculate(self, target: str | None = None) -> float:
        """目标函数值（缺省取 settings 的 targetFunction；非法表达式抛错）。"""
        try:
            value = self._wasm_backend().evaluate(
                self.s.get("charId"), self.s.get("settings") or {}, target
            ).get("targetValue")
        except WasmEvaluateError:
            raise
        except Exception as error:  # noqa: BLE001
            raise WasmEvaluateError(str(error)) from error
        return value if isinstance(value, float) and math.isfinite(value) else 0.0

    def evaluate(self, expr: str) -> float:
        """任意表达式求值（`calculate` 别名）。"""
        return self.calculate(expr)

    # ---------- 面板输出 ----------

    def char_panel(self) -> dict:
        """角色面板：全量角色属性（不含 weapon 键）。"""
        return {k: v for k, v in self._wasm_full_attrs().items() if k != "weapon"}

    def calculate_weapon_attributes(self) -> dict:
        """全量属性（含 weapon 面板键；调用方不得原地改写返回值）。"""
        cached = self._wasm_full_attrs()
        out = dict(cached)
        if isinstance(cached.get("weapon"), dict):
            out["weapon"] = dict(cached["weapon"])
        return out

    def weapon_panel(self, slot: str = "melee") -> dict | None:
        """武器面板：指定槽位武器的面板数值。

        slot 取 melee（近战）/ ranged（远程）/ skill（同律），或武器技能名；
        未知槽位返回 None。
        """
        norm = {"melee": "近战", "ranged": "远程", "skill": "同律"}.get(slot, slot)
        return self._copy_panel(self._wasm_all_panels().get(norm))

    # ---------- 技能等级/字段 ----------

    def skill_levels_final(self) -> list:
        """最终技能等级：[(技能名, 等级)]（trio + 溯源加成并钳制 1-12）。"""
        names = self.s.get("skillNames") or []
        finals = self.s.get("skillLevelsFinal") or []
        return [(name, finals[i] if i < len(finals) else None) for i, name in enumerate(names)]

    def skill_fields(self, skill_name: str) -> list:
        """技能字段面板数值（结算字段表；未知技能为空）。"""
        try:
            fields = self._wasm_backend().skill_fields(
                self.s.get("charId"), self.s.get("settings") or {}, skill_name
            )
        except WasmEvaluateError:
            raise
        except Exception as error:  # noqa: BLE001
            raise WasmEvaluateError(str(error)) from error
        out = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            item = {}
            for key, want in (
                ("name", "名称"), ("safeName", "safeName"), ("value", "值"),
                ("value2", "值2"), ("format", "格式"), ("base", "基础"),
                ("tags", "tag"), ("damageType", "伤害类型"),
            ):
                if field.get(key) is not None:
                    item[want] = field[key]
            out.append(item)
        return out
