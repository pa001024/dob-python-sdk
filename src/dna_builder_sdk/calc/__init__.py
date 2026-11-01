"""calc 包：BD 装配 + wasm 聚合引擎（无编辑语义）。"""

from .build import build_state, normalize_skill_levels, parse_trace_levels, resolve_skill_level
from .engine import Engine
from .gamedata import GameDataTables, TableSource, resolve_tables
from .wasm import WasmBackend, WasmEvaluateError, is_available as wasm_available

__all__ = [
    "Engine", "GameDataTables", "TableSource", "WasmBackend", "WasmEvaluateError",
    "build_state", "normalize_skill_levels", "resolve_skill_level", "parse_trace_levels",
    "resolve_tables", "wasm_available",
]
