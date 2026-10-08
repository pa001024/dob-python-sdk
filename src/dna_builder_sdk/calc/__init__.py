"""calc 包：BD 装配 + 聚合引擎 + 表达式/伤害结算（无编辑语义）。"""

from .ast import AstError, parse_ast, tokenize_ast
from .build import build_state, parse_trace_levels
from .damage import DamageContext
from .engine import Engine
from .evaluate import evaluate
from .gamedata import GameDataTables, TableSource, resolve_tables

__all__ = [
    "AstError", "DamageContext", "Engine", "GameDataTables", "TableSource",
    "build_state", "parse_trace_levels", "parse_ast", "tokenize_ast", "evaluate", "resolve_tables",
]
