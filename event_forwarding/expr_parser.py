"""Restricted-grammar expression parser that emits Spark Column trees.

Grammar (standard precedence, left to right for equal precedence)::

    expr    := term (('+' | '-') term)*
    term    := unary (('*' | '/') unary)*
    unary   := '-' unary | primary
    primary := NUMBER | FIELD | '(' expr ')'
    NUMBER  := [0-9]+ ('.' [0-9]+)?
    FIELD   := [A-Za-z_][A-Za-z0-9_]*

Nothing is evaluated in Python: the text is tokenised and parsed by hand into a small AST, and
the AST is translated into native Spark ``Column`` operations. There is no ``eval``, ``exec`` or UDF.

Semantics of the produced Column (Req 4.5, 4.6, 4.9):
- A field reference reads a top-level key of the raw ``data`` JSON string. The value is used only
  when it is a JSON number (a missing key, null, string, boolean, object or array gives null).
- Arithmetic is done in double precision.
- Division by zero gives null. A non-finite result gives null.
- Null propagates through every operator, so any failed operand nulls the whole expression.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pyspark.sql import Column
from pyspark.sql import functions as F

MAX_EXPRESSION_LENGTH = 500
MAX_NESTING_DEPTH = 50


class ExpressionError(ValueError):
    """Raised for text that is invalid or outside the restricted grammar."""

    def __init__(self, message: str, expression: str, position: int | None = None):
        self.expression = expression
        self.position = position
        where = f" at position {position}" if position is not None else ""
        super().__init__(f"{message}{where} in expression {expression!r}")


# ---------------------------------------------------------------- AST
@dataclass(frozen=True)
class Num:
    text: str


@dataclass(frozen=True)
class Field:
    name: str


@dataclass(frozen=True)
class Neg:
    operand: "Node"


@dataclass(frozen=True)
class BinOp:
    op: str  # one of + - * /
    left: "Node"
    right: "Node"


Node = Union[Num, Field, Neg, BinOp]


# ---------------------------------------------------------------- tokenizer
_DIGITS = "0123456789"
_IDENT_START = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
_IDENT_PART = _IDENT_START + _DIGITS
_OPERATORS = "+-*/()"


def _tokenize(text: str) -> list[tuple[str, str, int]]:
    """Return (kind, value, position) tokens; kind is num, field or op."""
    tokens: list[tuple[str, str, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
        elif ch in _DIGITS:
            start = i
            while i < n and text[i] in _DIGITS:
                i += 1
            if i < n and text[i] == ".":
                i += 1
                if i >= n or text[i] not in _DIGITS:
                    raise ExpressionError("malformed number", text, start)
                while i < n and text[i] in _DIGITS:
                    i += 1
            if i < n and (text[i] in _IDENT_PART or text[i] == "."):
                raise ExpressionError("malformed number", text, start)
            tokens.append(("num", text[start:i], start))
        elif ch in _IDENT_START:
            start = i
            while i < n and text[i] in _IDENT_PART:
                i += 1
            tokens.append(("field", text[start:i], start))
        elif ch in _OPERATORS:
            tokens.append(("op", ch, i))
            i += 1
        else:
            raise ExpressionError(f"unexpected character {ch!r}", text, i)
    return tokens


# ---------------------------------------------------------------- parser
class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0
        self.depth = 0

    def _peek(self) -> tuple[str, str, int] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _is_op(self, *ops: str) -> bool:
        tok = self._peek()
        return tok is not None and tok[0] == "op" and tok[1] in ops

    def _enter(self, position: int) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING_DEPTH:
            raise ExpressionError("expression nested too deeply", self.text, position)

    def parse(self) -> Node:
        if not self.tokens:
            raise ExpressionError("empty expression", self.text)
        node = self._expr()
        tok = self._peek()
        if tok is not None:
            raise ExpressionError(f"unexpected {tok[1]!r}", self.text, tok[2])
        return node

    def _expr(self) -> Node:
        node = self._term()
        while self._is_op("+", "-"):
            op = self.tokens[self.pos][1]
            self.pos += 1
            node = BinOp(op, node, self._term())
        return node

    def _term(self) -> Node:
        node = self._unary()
        while self._is_op("*", "/"):
            op = self.tokens[self.pos][1]
            self.pos += 1
            node = BinOp(op, node, self._unary())
        return node

    def _unary(self) -> Node:
        if self._is_op("-"):
            self._enter(self.tokens[self.pos][2])
            self.pos += 1
            try:
                return Neg(self._unary())
            finally:
                self.depth -= 1
        return self._primary()

    def _primary(self) -> Node:
        tok = self._peek()
        if tok is None:
            raise ExpressionError("unexpected end of expression", self.text, len(self.text))
        kind, value, position = tok
        if kind == "num":
            self.pos += 1
            return Num(value)
        if kind == "field":
            self.pos += 1
            return Field(value)
        if value == "(":
            self._enter(position)
            self.pos += 1
            node = self._expr()
            close = self._peek()
            if close is None or close[1] != ")":
                raise ExpressionError("missing closing parenthesis", self.text, position)
            self.pos += 1
            self.depth -= 1
            return node
        raise ExpressionError(f"unexpected {value!r}", self.text, position)


def parse_expression(text: str) -> Node:
    """Parse ``text`` into an AST. Raises ExpressionError for anything outside the grammar."""
    if not isinstance(text, str):
        raise ExpressionError("expression must be a string", repr(text))
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"expression longer than {MAX_EXPRESSION_LENGTH} characters", text[:40] + "...")
    return _Parser(text).parse()


def field_names(node: Node | str) -> list[str]:
    """Distinct field names referenced by an expression, in order of first use."""
    if isinstance(node, str):
        node = parse_expression(node)
    seen: dict[str, None] = {}

    def walk(n: Node) -> None:
        if isinstance(n, Field):
            seen.setdefault(n.name)
        elif isinstance(n, Neg):
            walk(n.operand)
        elif isinstance(n, BinOp):
            walk(n.left)
            walk(n.right)

    walk(node)
    return list(seen)


# ---------------------------------------------------------------- Column translation
def numeric_field(data: Column, name: str) -> Column:
    """Top-level key ``name`` of the JSON string ``data`` as a double, or null unless it is a JSON number."""
    variant = F.try_parse_json(data)
    value = F.try_variant_get(variant, f"$.{name}", "variant")
    type_name = F.schema_of_variant(value)
    is_number = type_name.isin("BIGINT", "DOUBLE") | type_name.startswith("DECIMAL")
    as_double = F.try_variant_get(variant, f"$.{name}", "double")
    finite = ~(F.isnan(as_double) | as_double.isin(float("inf"), float("-inf")))
    return F.when(is_number & finite, as_double)


def to_column(node: Node, data: Column) -> Column:
    """Translate an AST to a Spark Column evaluated against the raw JSON string column ``data``."""
    if isinstance(node, Num):
        return F.lit(float(node.text))
    if isinstance(node, Field):
        return numeric_field(data, node.name)
    if isinstance(node, Neg):
        return -to_column(node.operand, data)
    left = to_column(node.left, data)
    right = to_column(node.right, data)
    if node.op == "+":
        return left + right
    if node.op == "-":
        return left - right
    if node.op == "*":
        return left * right
    return F.when(right == F.lit(0.0), F.lit(None).cast("double")).otherwise(left / right)


def compile_expression(text: str, data: Column | str = "data") -> Column:
    """Parse ``text`` and return a double Column (null on any evaluation failure).

    ``data`` is the column (or column name) holding the raw JSON text of the event's ``data`` object.
    """
    node = parse_expression(text)
    data_col = F.col(data) if isinstance(data, str) else data
    result = to_column(node, data_col).cast("double")
    finite = ~(F.isnan(result) | result.isin(float("inf"), float("-inf")))
    return F.when(finite, result)
