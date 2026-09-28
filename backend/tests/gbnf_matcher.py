"""A tiny backtracking GBNF matcher, for tests only.

The whole reliability argument of this project rests on "with the grammar
active, malformed output is impossible". That claim deserves to be tested
rather than asserted, but llama.cpp is not available inside a unit test — so
this implements just enough of GBNF (the subset `app/llm/grammar.py` emits) to
decide whether a candidate string is derivable from a grammar.

Supported: `name ::= expr`, alternation, sequences, double-quoted literals,
character classes with ranges/negation/`\\xHH` escapes, rule references,
parenthesised groups, and the `*` `+` `?` `{m,n}` repetition suffixes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator


class GbnfSyntaxError(ValueError):
    pass


# ----------------------------------------------------------------- AST nodes


@dataclass
class Literal:
    text: str


@dataclass
class CharClass:
    negated: bool
    ranges: list[tuple[str, str]]

    def matches(self, ch: str) -> bool:
        inside = any(low <= ch <= high for low, high in self.ranges)
        return inside != self.negated


@dataclass
class RuleRef:
    name: str


@dataclass
class Sequence:
    items: list


@dataclass
class Alternation:
    options: list


@dataclass
class Repeat:
    item: object
    minimum: int
    maximum: int | None


# -------------------------------------------------------------------- parser

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<literal>"(?:\\.|[^"\\])*")
  | (?P<charclass>\[(?:\\.|[^\]\\])*\])
  | (?P<assign>::=)
  | (?P<pipe>\|)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<repeat>\{\d+(?:,\d*)?\})
  | (?P<suffix>[*+?])
  | (?P<name>[A-Za-z][A-Za-z0-9\-]*)
    """,
    re.VERBOSE,
)


def _tokenise(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    # Strip comments before tokenising.
    text = re.sub(r"#[^\n]*", "", text)
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise GbnfSyntaxError(f"cannot tokenise at {text[position:position + 30]!r}")
        position = match.end()
        kind = match.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, match.group()))
    return tokens


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "]": "]", "-": "-", "/": "/"}


def _unescape(raw: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(raw):
        ch = raw[index]
        if ch == "\\" and index + 1 < len(raw):
            nxt = raw[index + 1]
            if nxt == "x":
                out.append(chr(int(raw[index + 2 : index + 4], 16)))
                index += 4
                continue
            if nxt == "u":
                out.append(chr(int(raw[index + 2 : index + 6], 16)))
                index += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _parse_charclass(raw: str) -> CharClass:
    body = raw[1:-1]
    negated = body.startswith("^")
    if negated:
        body = body[1:]
    chars: list[str] = []
    index = 0
    while index < len(body):
        if body[index] == "\\":
            if body[index + 1] == "x":
                chars.append(chr(int(body[index + 2 : index + 4], 16)))
                index += 4
            elif body[index + 1] == "u":
                chars.append(chr(int(body[index + 2 : index + 6], 16)))
                index += 6
            else:
                chars.append(_ESCAPES.get(body[index + 1], body[index + 1]))
                index += 2
            chars.append("\x00SEP")  # marker so ranges can't span an escape pair
            continue
        chars.append(body[index])
        index += 1

    # Re-walk, honouring `a-z` ranges.
    ranges: list[tuple[str, str]] = []
    cleaned = [c for c in chars if c != "\x00SEP"]
    index = 0
    while index < len(cleaned):
        if index + 2 < len(cleaned) and cleaned[index + 1] == "-":
            ranges.append((cleaned[index], cleaned[index + 2]))
            index += 3
        else:
            ranges.append((cleaned[index], cleaned[index]))
            index += 1
    return CharClass(negated=negated, ranges=ranges)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse_grammar(self) -> dict[str, object]:
        rules: dict[str, object] = {}
        while self.peek():
            kind, value = self.next()
            if kind != "name":
                raise GbnfSyntaxError(f"expected rule name, got {value!r}")
            assign_kind, _ = self.next()
            if assign_kind != "assign":
                raise GbnfSyntaxError(f"expected ::= after {value}")
            rules[value] = self.parse_alternation()
        return rules

    def parse_alternation(self):
        options = [self.parse_sequence()]
        while True:
            token = self.peek()
            if token and token[0] == "pipe":
                self.next()
                options.append(self.parse_sequence())
            else:
                break
        return options[0] if len(options) == 1 else Alternation(options)

    def parse_sequence(self):
        items = []
        while True:
            token = self.peek()
            if token is None or token[0] in ("pipe", "rparen"):
                break
            # A `name ::=` pair starts the next rule, so stop before consuming it.
            if token[0] == "name" and self._next_is_assign():
                break
            items.append(self.parse_item())
        return Sequence(items)

    def _next_is_assign(self) -> bool:
        return (
            self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1][0] == "assign"
        )

    def parse_item(self):
        kind, value = self.next()
        if kind == "literal":
            node = Literal(_unescape(value[1:-1]))
        elif kind == "charclass":
            node = _parse_charclass(value)
        elif kind == "name":
            node = RuleRef(value)
        elif kind == "lparen":
            node = self.parse_alternation()
            close_kind, _ = self.next()
            if close_kind != "rparen":
                raise GbnfSyntaxError("unbalanced parenthesis")
        else:
            raise GbnfSyntaxError(f"unexpected token {value!r}")

        token = self.peek()
        if token and token[0] == "suffix":
            self.next()
            if token[1] == "*":
                return Repeat(node, 0, None)
            if token[1] == "+":
                return Repeat(node, 1, None)
            return Repeat(node, 0, 1)
        if token and token[0] == "repeat":
            self.next()
            body = token[1][1:-1]
            if "," in body:
                low, high = body.split(",", 1)
                return Repeat(node, int(low), int(high) if high else None)
            return Repeat(node, int(body), int(body))
        return node


def parse_gbnf(text: str) -> dict[str, object]:
    return _Parser(_tokenise(text)).parse_grammar()


# ------------------------------------------------------------------ matching

#: Guards against pathological backtracking on a malformed grammar.
MAX_REPEAT = 64


class Matcher:
    def __init__(self, grammar_text: str) -> None:
        self.rules = parse_gbnf(grammar_text)
        if "root" not in self.rules:
            raise GbnfSyntaxError("grammar has no root rule")

    def matches(self, text: str) -> bool:
        return any(end == len(text) for end in self._match(self.rules["root"], text, 0))

    def _match(self, node, text: str, pos: int) -> Iterator[int]:
        if isinstance(node, Literal):
            if text.startswith(node.text, pos):
                yield pos + len(node.text)
            return

        if isinstance(node, CharClass):
            if pos < len(text) and node.matches(text[pos]):
                yield pos + 1
            return

        if isinstance(node, RuleRef):
            target = self.rules.get(node.name)
            if target is None:
                raise GbnfSyntaxError(f"undefined rule {node.name}")
            yield from self._match(target, text, pos)
            return

        if isinstance(node, Alternation):
            for option in node.options:
                yield from self._match(option, text, pos)
            return

        if isinstance(node, Sequence):
            yield from self._match_sequence(node.items, text, pos)
            return

        if isinstance(node, Repeat):
            yield from self._match_repeat(node, text, pos, 0)
            return

        raise GbnfSyntaxError(f"unknown node {node!r}")

    def _match_sequence(self, items, text: str, pos: int) -> Iterator[int]:
        if not items:
            yield pos
            return
        head, rest = items[0], items[1:]
        for after_head in self._match(head, text, pos):
            yield from self._match_sequence(rest, text, after_head)

    def _match_repeat(self, node: Repeat, text: str, pos: int, count: int) -> Iterator[int]:
        maximum = node.maximum if node.maximum is not None else MAX_REPEAT
        if count >= node.minimum:
            yield pos
        if count >= maximum:
            return
        for after in self._match(node.item, text, pos):
            if after == pos:  # zero-width match; stop or we loop forever
                return
            yield from self._match_repeat(node, text, after, count + 1)
