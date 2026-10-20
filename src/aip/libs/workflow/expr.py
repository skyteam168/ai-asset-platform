"""condition 节点的表达式：CEL 子集（architecture.md 6.3）。

只支持数据比较，不能调用函数、不能写入：
  或 ||、与 &&、非 !、比较 == != < <= > >=、成员 in、括号、列表字面量 [..]、
  字符串 '..' / "..."、数字、true / false / null、数据路径 input.amount、n2.risk_level、n1.items[0].qty。
路径不存在时取 null（确定性：只依赖已记录的步骤输出）。
"""

from __future__ import annotations

import re
from typing import Any

from aip.libs.common.errors import ErrorCode, PlatformError

TOKEN = re.compile(
    r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\],.])|(?P<id>[A-Za-z_][A-Za-z0-9_]*))"
)
CMP = ("==", "!=", "<", "<=", ">", ">=", "in")
MAX_LEN = 500

Node = tuple[Any, ...]


def _bad(msg: str) -> PlatformError:
    return PlatformError(ErrorCode.INVALID_ARGUMENT, f"条件表达式错误：{msg}")


def tokenize(src: str) -> list[tuple[str, str]]:
    if len(src) > MAX_LEN:
        raise _bad("过长")
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        if src[pos:].strip() == "":
            break
        m = TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise _bad(f"无法识别 {src[pos : pos + 10]!r}")
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _unescape(s: str) -> str:
    """字符串字面量只支持反斜杠转义下一个字符（\\' \\" \\\\），其余原样保留，不做 unicode_escape。"""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t, self.i = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise _bad(f"缺少 {value or '内容'}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        node = self.or_()
        if self.peek() is not None:
            raise _bad(f"多余的 {self.peek()[1]!r}")  # type: ignore[index]
        return node

    def or_(self) -> Node:
        left = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            left = ("or", left, self.and_())
        return left

    def and_(self) -> Node:
        left = self.not_()
        while self.peek() == ("op", "&&"):
            self.take()
            left = ("and", left, self.not_())
        return left

    def not_(self) -> Node:
        if self.peek() == ("op", "!"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self) -> Node:
        left = self.atom()
        tok = self.peek()
        if tok and tok[1] in CMP:
            self.take()
            return ("cmp", tok[1], left, self.atom())
        return left

    def atom(self) -> Node:
        tok = self.take()
        kind, val = tok
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            return ("lit", _unescape(val[1:-1]))
        if val == "(":
            node = self.or_()
            self.take(")")
            return node
        if val == "[":
            items: list[Node] = []
            if self.peek() != ("op", "]"):
                items.append(self.atom())
                while self.peek() == ("op", ","):
                    self.take()
                    items.append(self.atom())
            self.take("]")
            return ("list", items)
        if kind == "id":
            if val in ("true", "false", "null"):
                return ("lit", {"true": True, "false": False, "null": None}[val])
            path: list[str | int] = [val]
            while self.peek() in (("op", "."), ("op", "[")):
                if self.take()[1] == ".":
                    k, name = self.take()
                    if k != "id":
                        raise _bad("路径段必须是标识符")
                    path.append(name)
                else:
                    k, num = self.take()
                    if k != "num" or "." in num or num.startswith("-"):
                        raise _bad("下标必须是非负整数")
                    path.append(int(num))
                    self.take("]")
            return ("path", path)
        raise _bad(f"意外的 {val!r}")


def parse(src: str) -> Node:
    return _Parser(tokenize(src)).parse()


def roots(node: Node) -> set[str]:
    """表达式引用的根名（input 或节点 id），供静态校验检查引用是否已定义。"""
    tag = node[0]
    if tag == "path":
        return {str(node[1][0])}
    if tag == "lit":
        return set()
    if tag == "list":
        return set().union(*(roots(x) for x in node[1])) if node[1] else set()
    if tag == "not":
        return roots(node[1])
    if tag == "cmp":
        return roots(node[2]) | roots(node[3])
    return roots(node[1]) | roots(node[2])


def lookup(ctx: dict[str, Any], path: list[str | int]) -> Any:
    cur: Any = ctx
    for part in path:
        if isinstance(part, int):
            cur = cur[part] if isinstance(cur, list) and 0 <= part < len(cur) else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def evaluate(node: Node, ctx: dict[str, Any]) -> Any:
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "path":
        return lookup(ctx, node[1])
    if tag == "list":
        return [evaluate(x, ctx) for x in node[1]]
    if tag == "not":
        return not truthy(evaluate(node[1], ctx))
    if tag == "and":
        return truthy(evaluate(node[1], ctx)) and truthy(evaluate(node[2], ctx))
    if tag == "or":
        return truthy(evaluate(node[1], ctx)) or truthy(evaluate(node[2], ctx))
    op, a, b = node[1], evaluate(node[2], ctx), evaluate(node[3], ctx)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "in":
        return isinstance(b, list | str | dict) and a in b
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return _order(op, float(a), float(b))
    if isinstance(a, str) and isinstance(b, str):
        return _order(op, a, b)
    return False  # 类型不可比较时为假，不抛错


def _order(op: str, a: Any, b: Any) -> bool:
    return bool({"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])


def truthy(v: Any) -> bool:
    return v is True


def check(src: str, ctx: dict[str, Any]) -> bool:
    return truthy(evaluate(parse(src), ctx))
