"""BUFF 动态 `code` 的 JS 子集解释器。

TS 侧以 `new Function("attr", `with(attr){code;return attr}`)` 执行，
语料（`src/data/d/buff.data.ts` 共 14 段）只用到：`var`、`if/else`、
块、表达式语句、`?:`、`||`、`&&`、`==/!=/===/!==`、比较、四则、`%`、
一元 `-`/`!`、`Math.floor/min/max/ceil/abs/round/pow/sqrt/log`、
成员读写链、赋值（`=,+=,-=,*=,/=,%=`）、逗号序列。

作用域复刻 `with` + sloppy 语义：读按 locals → sandbox；
写命中 sandbox 则写 sandbox（含嵌套 dict 引用），命中 locals 则写 locals，
否则进可丢弃的隐式全局；`undefined` 以 NaN 参与算术（与 JS 一致传播）。
除零/非法 Math 输入按 JS 语义返回 inf/NaN，不抛错。
"""

from __future__ import annotations

import math

NAN = float("nan")


def _is_truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return len(value) > 0
    return True


def _js_eq(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return _num(left) == _num(right)
    if left is None or (isinstance(left, float) and math.isnan(left)):
        return False
    if right is None or (isinstance(right, float) and math.isnan(right)):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return left == right


def _num(value) -> float:
    if value is None:
        return NAN
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return NAN
    return NAN


def _safe_div(left: float, right: float) -> float:
    if right == 0:
        if left == 0 or (isinstance(left, float) and math.isnan(left)):
            return NAN
        return math.inf if left > 0 else -math.inf
    return left / right


def _safe_mod(left: float, right: float) -> float:
    if right == 0 or (isinstance(right, float) and math.isnan(right)):
        return NAN
    if isinstance(left, float) and (math.isnan(left) or math.isinf(left)):
        return NAN
    if isinstance(right, float) and math.isinf(right):
        return left
    return left % right if not (math.isinf(left)) else NAN


def _js_min(*args):
    for a in args:
        if isinstance(a, float) and math.isnan(a):
            return NAN
    return min(args)


def _js_max(*args):
    for a in args:
        if isinstance(a, float) and math.isnan(a):
            return NAN
    return max(args)


_MATH = {
    "floor": lambda *a: _math_total(math.floor, a[0]),
    "ceil": lambda *a: _math_total(math.ceil, a[0]),
    "round": lambda *a: _math_total(round, a[0]),
    "min": lambda *a: _js_min(*[_num(x) for x in a]),
    "max": lambda *a: _js_max(*[_num(x) for x in a]),
    "abs": lambda *a: abs(a[0]),
    "pow": lambda *a: _safe_pow(a[0], a[1]),
    "sqrt": lambda *a: math.sqrt(a[0]) if a[0] >= 0 else NAN,
    "log": lambda *a: math.log(a[0]) if a[0] > 0 else (NAN if a[0] == 0 else NAN),
}


def _math_total(func, x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return x
    try:
        return float(func(x))
    except (ValueError, OverflowError):
        return NAN


def _safe_pow(left, right):
    try:
        result = math.pow(left, right)
        return result
    except (ValueError, OverflowError, ZeroDivisionError):
        return NAN


class JsError(ValueError):
    pass


class _Tokenizer:
    THREE = ["===", "!=="]
    TWO = ["==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%="]

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.tokens: list[tuple[str, str]] = []
        self._run()
        self.tokens.append(("EOF", ""))

    def _run(self):
        text = self.text
        n = len(text)
        while self.pos < n:
            ch = text[self.pos]
            if ch.isspace():
                self.pos += 1
                continue
            three = text[self.pos : self.pos + 3]
            if three in self.THREE:
                self.tokens.append(("OP", three))
                self.pos += 3
                continue
            two = text[self.pos : self.pos + 2]
            if two in self.TWO:
                self.tokens.append(("OP", two))
                self.pos += 2
                continue
            if ch in "+-*/%<>=!?:;,(){}.":
                self.tokens.append(("OP", ch))
                self.pos += 1
                continue
            if ch.isdigit() or (ch == "." and self.pos + 1 < n and text[self.pos + 1].isdigit()):
                start = self.pos
                while self.pos < n and (text[self.pos].isdigit() or text[self.pos] in ".eE+-"):
                    # 允许科学计数法；多吃的 +- 只在 e/E 后合法，解析时由 float 校验
                    if text[self.pos] in "+-" and (self.pos == 0 or text[self.pos - 1] not in "eE"):
                        break
                    self.pos += 1
                self.tokens.append(("NUM", text[start : self.pos]))
                continue
            if ch == "'" or ch == '"':
                quote = ch
                self.pos += 1
                buf = ""
                while self.pos < n and text[self.pos] != quote:
                    if text[self.pos] == "\\" and self.pos + 1 < n:
                        buf += text[self.pos + 1]
                        self.pos += 2
                    else:
                        buf += text[self.pos]
                        self.pos += 1
                self.pos += 1
                self.tokens.append(("STR", buf))
                continue
            if ch.isalpha() or ch == "_" or ch == "$" or ord(ch) > 127:
                start = self.pos
                while self.pos < n and (text[self.pos].isalnum() or text[self.pos] in "_$" or ord(text[self.pos]) > 127):
                    self.pos += 1
                word = text[start : self.pos]
                self.tokens.append(("KW", word) if word in ("var", "if", "else", "true", "false", "undefined", "null") else ("ID", word))
                continue
            raise JsError(f"未知字符 {ch!r} 位于 {self.pos}")


class _Parser:
    def __init__(self, text: str):
        self.tokens = _Tokenizer(text).tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str, value: str | None = None):
        token = self.next()
        if token[0] != kind or (value is not None and token[1] != value):
            raise JsError(f"期望 {kind} {value or ''}，实际 {token}")
        return token

    def match(self, kind: str, value: str | None = None) -> bool:
        token = self.peek()
        if token[0] != kind or (value is not None and token[1] != value):
            return False
        self.pos += 1
        return True

    def parse_program(self):
        body = []
        while self.peek()[0] != "EOF":
            if self.peek() == ("OP", ";"):
                self.pos += 1
                continue
            body.append(self.parse_statement())
        return ("program", body)

    def parse_statement(self):
        token = self.peek()
        if token == ("KW", "var"):
            return self.parse_var()
        if token == ("KW", "if"):
            return self.parse_if()
        if token == ("OP", "{"):
            return self.parse_block()
        if token == ("OP", ";"):
            self.pos += 1
            return ("noop",)
        node = self.parse_expression()
        self.match("OP", ";")
        return ("expr", node)

    def parse_var(self):
        self.expect("KW", "var")
        declarators = []
        while True:
            name = self.expect("ID")[1]
            value = None
            if self.match("OP", "="):
                # 初始化器不含顶层逗号（逗号分隔声明器本身），与 parse_expression 区分
                value = self.parse_assign()
            declarators.append((name, value))
            if not self.match("OP", ","):
                break
        self.match("OP", ";")
        return ("var", declarators)

    def parse_if(self):
        self.expect("KW", "if")
        self.expect("OP", "(")
        cond = self.parse_expression()
        self.expect("OP", ")")
        then = self.parse_statement()
        otherwise = None
        if self.match("KW", "else"):
            otherwise = self.parse_statement()
        return ("if", cond, then, otherwise)

    def parse_block(self):
        self.expect("OP", "{")
        body = []
        while not self.match("OP", "}"):
            if self.peek()[0] == "EOF":
                raise JsError("块未闭合")
            body.append(self.parse_statement())
        return ("block", body)

    def parse_expression(self):
        return self.parse_comma()

    def parse_comma(self):
        node = self.parse_assign()
        while self.match("OP", ","):
            node = ("comma", node, self.parse_assign())
        return node

    def parse_assign(self):
        node = self.parse_conditional()
        token = self.peek()
        if token[0] == "OP" and token[1] in ("=", "+=", "-=", "*=", "/=", "%="):
            self.pos += 1
            if node[0] not in ("id", "member"):
                raise JsError("赋值目标非法")
            return ("assign", token[1], node, self.parse_assign())
        return node

    def parse_conditional(self):
        node = self.parse_or()
        if self.match("OP", "?"):
            then = self.parse_expression()
            self.expect("OP", ":")
            otherwise = self.parse_conditional()
            return ("cond", node, then, otherwise)
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.match("OP", "||"):
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_eq()
        while self.match("OP", "&&"):
            node = ("and", node, self.parse_eq())
        return node

    def parse_eq(self):
        node = self.parse_cmp()
        while True:
            token = self.peek()
            if token[0] == "OP" and token[1] in ("==", "!=", "===", "!=="):
                self.pos += 1
                node = ("eq", token[1], node, self.parse_cmp())
            else:
                return node

    def parse_cmp(self):
        node = self.parse_add()
        while True:
            token = self.peek()
            if token[0] == "OP" and token[1] in ("<", "<=", ">", ">="):
                self.pos += 1
                node = ("cmp", token[1], node, self.parse_add())
            else:
                return node

    def parse_add(self):
        node = self.parse_mul()
        while True:
            token = self.peek()
            if token[0] == "OP" and token[1] in ("+", "-"):
                self.pos += 1
                node = ("arith", token[1], node, self.parse_mul())
            else:
                return node

    def parse_mul(self):
        node = self.parse_unary()
        while True:
            token = self.peek()
            if token[0] == "OP" and token[1] in ("*", "/", "%"):
                self.pos += 1
                node = ("arith", token[1], node, self.parse_unary())
            else:
                return node

    def parse_unary(self):
        token = self.peek()
        if token[0] == "OP" and token[1] in ("-", "!", "+"):
            self.pos += 1
            return ("unary", token[1], self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        node = self.parse_primary()
        while True:
            token = self.peek()
            if token == ("OP", "."):
                self.pos += 1
                prop = self.expect("ID")[1]
                node = ("member", node, prop)
            elif token == ("OP", "("):
                self.pos += 1
                args = []
                if not (self.peek()[0] == "OP" and self.peek()[1] == ")"):
                    while True:
                        # 参数间逗号是分隔符：按赋值表达式解析（不含顶层逗号）
                        args.append(self.parse_assign())
                        if not self.match("OP", ","):
                            break
                self.expect("OP", ")")
                node = ("call", node, args)
            else:
                return node

    def parse_primary(self):
        token = self.next()
        if token[0] == "NUM":
            try:
                return ("num", float(token[1]))
            except ValueError:
                raise JsError(f"非法数字 {token[1]!r}")
        if token[0] == "STR":
            return ("str", token[1])
        if token == ("KW", "true"):
            return ("num", 1.0)
        if token == ("KW", "false"):
            return ("num", 0.0)
        if token == ("KW", "undefined") or token == ("KW", "null"):
            return ("num", NAN)
        if token[0] == "ID":
            return ("id", token[1])
        if token == ("OP", "("):
            node = self.parse_expression()
            self.expect("OP", ")")
            return node
        raise JsError(f"意外的标记 {token}")


class _Scope:
    """locals（var）+ sandbox（with 对象）+ 可丢弃隐式全局。"""

    def __init__(self, sandbox: dict):
        self.locals: dict = {}
        self.sandbox = sandbox
        self.implicit: dict = {}

    def read(self, name: str):
        # 复刻 with(attr)：对象属性优先于 var 声明，再到 Math 与隐式全局
        if name in self.sandbox:
            return self.sandbox[name]
        if name in self.locals:
            return self.locals[name]
        if name == "Math":
            return _MATH
        if name in self.implicit:
            return self.implicit[name]
        return NAN

    def write(self, name: str, value):
        if name in self.sandbox:
            self.sandbox[name] = value
        elif name in self.locals:
            self.locals[name] = value
        else:
            # sloppy 隐式全局：调用方只取回 sandbox，本字典自然丢弃
            self.implicit[name] = value


def _resolve_member(scope: _Scope, node) -> tuple:
    """解析成员链根：返回 (容器, 键)。根 id 经作用域读取（Math 等内建同理）。"""
    if node[0] == "id":
        name = node[1]
        if name in scope.sandbox:
            return (scope.sandbox, name)
        if name in scope.locals:
            return (scope.locals, name)
        if name == "Math":
            return (_MATH, None)
        if name in scope.implicit:
            return (scope.implicit, name)
        return (None, name)
    if node[0] == "member":
        container = _eval(scope, node[1])
        return (container, node[2])
    raise JsError("成员链根非法")


def _member_get(container, key):
    if container is None:
        raise JsError(f"读取 undefined 的属性: {key}")
    if isinstance(container, dict):
        value = container.get(key, NAN)
        if value is None:
            return NAN
        return value
    return getattr(container, key, NAN)


def _member_set(container, key, value):
    if isinstance(container, dict):
        container[key] = value
    elif container is not None:
        setattr(container, key, value)


def _eval(scope: _Scope, node):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "id":
        return scope.read(node[1])
    if kind == "member":
        return _member_get(_eval(scope, node[1]), node[2])
    if kind == "call":
        target = node[1]
        args = [_eval(scope, arg) for arg in node[2]]
        if target[0] == "member" and target[1] == ("id", "Math"):
            func = _MATH.get(target[2])
            if func is None:
                raise JsError(f"不支持的 Math 方法: {target[2]}")
            return func(*[_num(a) for a in args])
        raise JsError("仅支持 Math.* 调用")
    if kind == "unary":
        value = _eval(scope, node[2])
        if node[1] == "-":
            return -_num(value)
        if node[1] == "+":
            return _num(value)
        return 0.0 if _is_truthy(value) else 1.0
    if kind == "arith":
        left, right = _num(_eval(scope, node[2])), _num(_eval(scope, node[3]))
        op = node[1]
        if op == "+":
            # 语料无字符串拼接；一律数值加
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return _safe_div(left, right)
        return _safe_mod(left, right)
    if kind == "cmp":
        left, right = _num(_eval(scope, node[2])), _num(_eval(scope, node[3]))
        op = node[1]
        if math.isnan(left) or math.isnan(right):
            return 0.0
        return 1.0 if {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op] else 0.0
    if kind == "eq":
        result = _js_eq(_eval(scope, node[2]), _eval(scope, node[3]))
        return 1.0 if (result if node[1] in ("==", "===") else not result) else 0.0
    if kind == "or":
        left = _eval(scope, node[1])
        return left if _is_truthy(left) else _eval(scope, node[2])
    if kind == "and":
        left = _eval(scope, node[1])
        return _eval(scope, node[2]) if _is_truthy(left) else left
    if kind == "cond":
        return _eval(scope, node[2]) if _is_truthy(_eval(scope, node[1])) else _eval(scope, node[3])
    if kind == "comma":
        _eval(scope, node[1])
        return _eval(scope, node[2])
    if kind == "assign":
        _, op, target, expr = node
        value = _eval(scope, expr)
        if target[0] == "id":
            name = target[1]
            if op != "=":
                current = _num(scope.read(name))
                value = _apply_compound(op, current, _num(value))
            scope.write(name, value)
            return value
        if target[0] == "member":
            # 成员赋值：容器取链根的值（如 skillWeaponAttr 面板 dict），键为末端属性
            container = _eval(scope, target[1])
            key = target[2]
        else:
            container, key = _resolve_member(scope, target)
            if key is None:
                raise JsError("赋值目标非法")
        current = _num(_member_get(container, key))
        if op != "=":
            value = _apply_compound(op, current, _num(value))
        if container is None:
            scope.write(key if isinstance(target[1], str) else key, value)
        else:
            _member_set(container, key, value)
        return value
    raise JsError(f"未知节点 {kind}")


def _apply_compound(op: str, current: float, value: float) -> float:
    if op == "+=":
        return current + value
    if op == "-=":
        return current - value
    if op == "*=":
        return current * value
    if op == "/=":
        return _safe_div(current, value)
    return _safe_mod(current, value)


def _exec(scope: _Scope, node):
    kind = node[0]
    if kind == "program":
        for stmt in node[1]:
            _exec(scope, stmt)
        return
    if kind == "noop":
        return
    if kind == "block":
        for stmt in node[1]:
            _exec(scope, stmt)
        return
    if kind == "var":
        for name, value in node[1]:
            scope.locals[name] = _eval(scope, value) if value is not None else NAN
        return
    if kind == "if":
        _, cond, then, otherwise = node
        if _is_truthy(_eval(scope, cond)):
            _exec(scope, then)
        elif otherwise is not None:
            _exec(scope, otherwise)
        return
    if kind == "expr":
        _eval(scope, node[1])
        return
    raise JsError(f"未知语句 {kind}")


def run_js_code(code: str, sandbox: dict) -> dict:
    """执行一段 BUFF code，返回执行后的 sandbox（原地修改并返回）。"""
    program = _Parser(code).parse_program()
    _exec(_Scope(sandbox), program)
    return sandbox
