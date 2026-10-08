"""AST 词法/语法：逐行对齐 src/data/ast.ts（Tokenizer + Parser）。

节点为普通 dict，type 取值：binary / unary / property / function /
member_access / temporary_attributes / number，与 TS 字段名一致
（含 namespace / forceAttr / skillContext 占位，后者 SDK 不绑定）。
"""

from __future__ import annotations

import re


class AstError(ValueError):
    pass


# JS parseFloat 可接受的最长数字前缀（TS parseFactor 用 parseFloat 转 NUMBER token，
# 如 parseFloat("0.0.0039") === 0；Python float() 严格语义会抛，必须镜像）
_JS_FLOAT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def js_parse_float(text: str) -> float:
    """镜像 JS parseFloat：取最长合法前缀，无有效前缀返回 NaN。"""
    s = text.lstrip()
    rest = s[1:] if s[:1] in ("+", "-") else s
    if rest.startswith("Infinity"):
        return float("-inf") if s[:1] == "-" else float("inf")
    match = _JS_FLOAT_RE.match(s)
    if match is None:
        return float("nan")
    try:
        return float(match.group(0))
    except ValueError:
        return float("nan")


class _Tokenizer:
    def __init__(self, text: str, macros: dict[str, str]):
        self.text = text
        self.pos = 0
        self.macros = macros

    def next_token(self):
        self._skip_ws()
        if self.pos >= len(self.text):
            return {"kind": "EOF", "value": "", "pos": self.pos}
        ch = self.text[self.pos]
        if ch.isdigit():
            start = self.pos
            buf = ""
            while self.pos < len(self.text) and (self.text[self.pos].isdigit() or self.text[self.pos] == "."):
                buf += self.text[self.pos]
                self.pos += 1
            return {"kind": "NUMBER", "value": buf, "pos": start}
        if ch.isalpha() or ch == "_" or ch == "[" or "\u4e00" <= ch <= "\u9fa5" or ch == "·":
            start = self.pos
            buf = ""
            while self.pos < len(self.text):
                c = self.text[self.pos]
                if c.isalnum() or c == "_" or "\u4e00" <= c <= "\u9fa5" or c in ("·", "[", "]"):
                    buf += c
                    self.pos += 1
                else:
                    break
            if buf in self.macros:
                repl = self.macros[buf]
                self.text = self.text[: start] + repl + self.text[self.pos :]
                self.pos = start
                return self.next_token()
            return {"kind": "IDENT", "value": buf, "pos": start}
        if ch == "/" and self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "/":
            self.pos += 2
            return {"kind": "OP", "value": "//", "pos": self.pos - 2}
        if ch in ("+", "-", "*", "/", "%"):
            self.pos += 1
            return {"kind": "OP", "value": ch, "pos": self.pos - 1}
        if ch == ".":
            self.pos += 1
            return {"kind": "DOT", "value": ".", "pos": self.pos - 1}
        if ch == ":":
            if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == ":":
                self.pos += 2
                return {"kind": "DCOLON", "value": "::", "pos": self.pos - 2}
            self.pos += 1
            return {"kind": "COLON", "value": ":", "pos": self.pos - 1}
        simple = {"(": "LPAREN", ")": "RPAREN", ",": "COMMA", "{": "LBRACE", "}": "RBRACE", "!": "BANG"}
        if ch in simple:
            self.pos += 1
            return {"kind": simple[ch], "value": ch, "pos": self.pos - 1}
        raise AstError(f"未知字符 '{ch}' 位于位置 {self.pos}")

    def _skip_ws(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1


class _Parser:
    def __init__(self, text: str, macros: dict[str, str]):
        tok = _Tokenizer(text, macros)
        self.tokens: list[dict] = []
        while True:
            t = tok.next_token()
            if t["kind"] == "EOF":
                break
            self.tokens.append(t)
        self.cur = 0

    def parse(self):
        if not self.tokens:
            raise AstError("表达式为空")
        node = self._expr()
        if self.cur < len(self.tokens):
            if self._peek()["kind"] == "COLON":
                raise AstError("单个冒号 ':' 不支持,请使用 '::' 进行命名空间访问")
            raise AstError(f"表达式末尾发现意外的标记 '{self._peek()['value']}'")
        return node

    def _expr(self):
        left = self._term()
        while self._match("OP", "+") or self._match("OP", "-"):
            op = self._prev()["value"]
            if self._at_end() or self._check("OP") or self._check("RPAREN") or self._check("COMMA"):
                raise AstError(f"运算符 '{op}' 后缺少操作数")
            left = {"type": "binary", "operator": op, "left": left, "right": self._term()}
        return left

    def _term(self):
        left = self._unary()
        while (
            self._match("OP", "*") or self._match("OP", "/") or self._match("OP", "//") or self._match("OP", "%")
        ):
            op = self._prev()["value"]
            if self._at_end() or self._check("OP") or self._check("RPAREN") or self._check("COMMA"):
                raise AstError(f"运算符 '{op}' 后缺少操作数")
            left = {"type": "binary", "operator": op, "left": left, "right": self._unary()}
        return left

    def _unary(self):
        if self._match("OP", "-") or self._match("OP", "+"):
            return {"type": "unary", "operator": self._prev()["value"], "argument": self._unary()}
        return self._factor()

    def _factor(self):
        if self._match("NUMBER"):
            node: dict = {"type": "number", "value": js_parse_float(self._prev()["value"])}
        elif self._match("IDENT"):
            name = self._prev()["value"]
            ns = None
            if self._match("DCOLON"):
                ns = name
                nxt = self._consume("IDENT", "命名空间 '::' 后缺少标识符")
                name = nxt["value"]
                if self._match("LPAREN"):
                    node = {"type": "function", "name": name, "namespace": ns, "args": self._arg_list()}
                else:
                    node = {"type": "property", "name": name, "namespace": ns}
                    node = self._force_suffix(node)
            elif self._match("LPAREN"):
                node = {"type": "function", "name": name, "args": self._arg_list()}
            else:
                node = {"type": "property", "name": name}
                node = self._force_suffix(node)
        elif self._match("LPAREN"):
            node = self._expr()
            self._consume("RPAREN", "表达式后缺少 ')'")
        else:
            raise AstError(f"意外的标记: {self._peek()['value']}")
        while True:
            if self._match("DOT"):
                prop = self._consume("IDENT", "成员访问 '.' 后缺少属性名称")
                node = {"type": "member_access", "object": node, "property": prop["value"]}
                continue
            if self._match("LBRACE"):
                if node["type"] not in ("property", "member_access", "temporary_attributes"):
                    raise AstError("临时属性只能应用于字段")
                attrs: list[dict] = []
                seen: set[str] = set()
                if self._check("RBRACE"):
                    raise AstError("临时属性不能为空")
                while True:
                    a = self._consume("IDENT", "临时属性缺少属性名")
                    self._consume("COLON", f"临时属性 '{a['value']}' 后缺少 ':'")
                    if a["value"] in seen:
                        raise AstError(f"临时属性 '{a['value']}' 重复")
                    seen.add(a["value"])
                    attrs.append({"name": a["value"], "value": self._expr()})
                    if not self._match("COMMA"):
                        break
                self._consume("RBRACE", "临时属性后缺少 '}'")
                node = {"type": "temporary_attributes", "target": node, "attributes": attrs}
                continue
            break
        return node

    def _arg_list(self):
        args = []
        if not self._check("RPAREN"):
            while True:
                args.append(self._expr())
                if not self._match("COMMA"):
                    break
        self._consume("RPAREN", "函数参数后缺少 ')'")
        return args

    def _force_suffix(self, node):
        if self._match("BANG"):
            node = {**node, "forceAttr": True}
        return node

    # -- 游标 --
    def _match(self, kind, value=None):
        if self._check(kind, value):
            self.cur += 1
            return True
        return False

    def _check(self, kind, value=None):
        if self._at_end():
            return False
        t = self._peek()
        if t["kind"] != kind:
            return False
        return value is None or t["value"] == value

    def _at_end(self):
        return self.cur >= len(self.tokens)

    def _peek(self):
        return self.tokens[self.cur] if self.cur < len(self.tokens) else {"kind": "EOF", "value": ""}

    def _prev(self):
        return self.tokens[self.cur - 1]

    def _consume(self, kind, message):
        if self._check(kind):
            self.cur += 1
            return self._prev()
        raise AstError(message)


def _normalize_macros(macros) -> dict[str, str]:
    if not macros:
        return {}
    if isinstance(macros, dict):
        return dict(macros)
    raise AstError("macros 仅支持 dict（TS 侧 Map/Record 口径）")


# CharBuild.macros：AST 表达式宏替换（TS 侧每次 parseAST 传入）
CHAR_MACROS = {
    "ATK": "攻击", "DEF": "防御", "HP": "生命", "SP": "神智",
    "DPH": "or(多重,1)*伤害", "总伤": "max(1,召唤物攻击次数)*伤害",
    "暴击伤害": "伤害.暴击", "DPS": "or(攻速,1+技能速度)*or(多重,1)*伤害",
    "范围收益": "技能范围*伤害", "耐久收益": "技能耐久*伤害", "效益收益": "技能效益*伤害",
    "每神智DPH": "1/神智消耗*伤害", "每持续神智DPH": "1/每秒神智消耗*伤害",
    "每神智DPS": "or(攻速,1+技能速度)/神智消耗*伤害",
    "每持续神智DPS": "or(攻速,1+技能速度)/每秒神智消耗*伤害",
}


def parse_ast(expr: str, macros: dict[str, str] | None = None) -> dict:
    """解析表达式为 AST dict（缺省沿用 CharBuild.macros，与 TS 各调用点一致）。"""
    return _Parser(expr, _normalize_macros(CHAR_MACROS if macros is None else macros)).parse()


def tokenize_ast(expr: str, max_length: int | None = None) -> list[dict]:
    """按 AST 词法扫描（宏名原样返回，不替换），供位置扫描使用。"""
    limit = max_length if max_length is not None else float("inf")
    tok = _Tokenizer(expr, {})
    out = []
    while True:
        t = tok.next_token()
        if t["kind"] == "EOF" or t["pos"] >= limit:
            break
        out.append(t)
    return out
