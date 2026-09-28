"""JSON Schema → GBNF.

This is the reliability core of the system. Rather than asking a local model for
JSON and hoping, we hand llama.cpp a grammar: the sampler is only ever allowed
to emit tokens that continue a valid derivation of the schema, so malformed
output is not unlikely, it is *impossible*.

Two honest caveats, kept in view on purpose:

* A grammar guarantees the *shape* of the answer, never its *quality*. There is
  evidence that hard schema constraints can slightly degrade reasoning about
  which tool to call and with what arguments, so this complements a model with
  good tool instincts, it does not replace one.
* Numeric bounds (`minimum`/`maximum`) are not expressible in GBNF. They are
  enforced by Pydantic after generation instead; see `llm/schemas.py`.

Supported: object (fixed key order, all keys required), array (with
`minItems`/`maxItems`), string (with `enum`), number, integer, boolean, null,
`$ref`/`$defs`, and `anyOf` as alternation.
"""

from __future__ import annotations

import json
import re
from typing import Any

PRIMITIVE_RULES = {
    "ws": r"ws ::= [ \t\n]*",
    "string": (
        r'string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" (["\\bfnrt/] | "u" [0-9a-fA-F]{4}) )* "\""'
    ),
    "number": r'number ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?',
    "integer": r'integer ::= "-"? ("0" | [1-9] [0-9]*)',
    "boolean": r'boolean ::= "true" | "false"',
    "null": r'null ::= "null"',
}

#: Generous but finite, so a confused model cannot generate forever.
DEFAULT_MAX_ITEMS = 8


class GrammarError(ValueError):
    pass


def _gbnf_literal(raw: str) -> str:
    """Wrap raw output text as a GBNF double-quoted literal."""
    out = raw.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _json_literal(value: Any) -> str:
    """GBNF literal that emits `value` as JSON.

    Note the double layer: a string key becomes the JSON text `"symbol"`, which
    then has to be escaped again to sit inside a GBNF literal, giving
    `"\\"symbol\\""`. Getting this wrong produces a grammar that compiles fine
    and emits unparseable JSON, so it is exercised directly in the tests.
    """
    return _gbnf_literal(json.dumps(value))


def _rule_name(hint: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", hint).strip("-").lower() or "rule"
    if not base[0].isalpha():
        base = f"r-{base}"
    name = base
    counter = 2
    while name in taken:
        name = f"{base}-{counter}"
        counter += 1
    taken.add(name)
    return name


class _GrammarBuilder:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.root_schema = schema
        self.defs: dict[str, Any] = {}
        for key in ("$defs", "definitions"):
            self.defs.update(schema.get(key) or {})
        self.rules: dict[str, str] = {}
        self.taken: set[str] = {"root", *PRIMITIVE_RULES}
        self.used_primitives: set[str] = {"ws"}
        self._ref_cache: dict[str, str] = {}

    # ------------------------------------------------------------- helpers
    def _resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        seen = set()
        while "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                raise GrammarError(f"circular $ref: {ref}")
            seen.add(ref)
            name = ref.rsplit("/", 1)[-1]
            if name not in self.defs:
                raise GrammarError(f"unresolvable $ref: {ref}")
            schema = self.defs[name]
        return schema

    def _primitive(self, name: str) -> str:
        self.used_primitives.add(name)
        return name

    def _add_rule(self, hint: str, body: str) -> str:
        name = _rule_name(hint, self.taken)
        self.rules[name] = body
        return name

    # -------------------------------------------------------------- visitor
    def visit(self, schema: dict[str, Any], hint: str) -> str:
        """Return the name of a rule (or an inline primitive) matching `schema`."""
        if "$ref" in schema:
            ref = schema["$ref"]
            if ref in self._ref_cache:
                return self._ref_cache[ref]
            resolved = self._resolve(schema)
            name = self.visit(resolved, ref.rsplit("/", 1)[-1])
            self._ref_cache[ref] = name
            return name

        if "enum" in schema:
            alternatives = " | ".join(_json_literal(v) for v in schema["enum"])
            return self._add_rule(f"{hint}-enum", alternatives)

        if "const" in schema:
            return self._add_rule(f"{hint}-const", _json_literal(schema["const"]))

        if "anyOf" in schema or "oneOf" in schema:
            options = schema.get("anyOf") or schema.get("oneOf")
            # `X | null` from Optional[...] is by far the common case.
            names = [self.visit(o, f"{hint}-opt{i}") for i, o in enumerate(options)]
            return self._add_rule(f"{hint}-any", " | ".join(names))

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            names = [self.visit({**schema, "type": t}, f"{hint}-{t}") for t in schema_type]
            return self._add_rule(f"{hint}-union", " | ".join(names))

        if schema_type == "object":
            return self._visit_object(schema, hint)
        if schema_type == "array":
            return self._visit_array(schema, hint)
        if schema_type == "string":
            return self._primitive("string")
        if schema_type == "number":
            return self._primitive("number")
        if schema_type == "integer":
            return self._primitive("integer")
        if schema_type == "boolean":
            return self._primitive("boolean")
        if schema_type == "null":
            return self._primitive("null")

        raise GrammarError(f"unsupported schema at {hint!r}: {schema}")

    def _visit_object(self, schema: dict[str, Any], hint: str) -> str:
        properties: dict[str, Any] = schema.get("properties") or {}
        if not properties:
            # Free-form object: allow the empty object only, rather than
            # opening a hole in the grammar.
            return self._add_rule(f"{hint}-obj", '"{" ws "}"')

        required = schema.get("required")
        # Every declared key is emitted, in declaration order. Fixed key order
        # is what makes the grammar small and the output diffable; Pydantic
        # defaults cover anything the schema marks optional.
        keys = list(properties.keys()) if required is None else [
            k for k in properties if k in required
        ] or list(properties.keys())

        parts: list[str] = ['"{" ws']
        for index, key in enumerate(keys):
            value_rule = self.visit(properties[key], f"{hint}-{key}")
            # `ws` on both sides of the comma: the grammar should never be the
            # reason a high-probability token is blocked, and permitting
            # optional whitespace costs nothing.
            separator = ' ws "," ws ' if index else " "
            parts.append(f'{separator}{_json_literal(key)} ws ":" ws {value_rule}')
        parts.append(' ws "}"')
        return self._add_rule(f"{hint}-obj", "".join(parts))

    def _visit_array(self, schema: dict[str, Any], hint: str) -> str:
        items = schema.get("items")
        if not items:
            return self._add_rule(f"{hint}-arr", '"[" ws "]"')

        item_rule = self.visit(items, f"{hint}-item")
        min_items = int(schema.get("minItems", 0))
        max_items = int(schema.get("maxItems", DEFAULT_MAX_ITEMS))
        if max_items < min_items:
            raise GrammarError(f"maxItems < minItems at {hint!r}")

        # Repetition is expressed on the ", item" tail, so the counts describe
        # elements after the first.
        tail = f'(ws "," ws {item_rule})'
        extra = max_items - 1
        if extra <= 0:
            body_with_items = f"{item_rule}"
        elif min_items <= 1:
            body_with_items = f"{item_rule} {tail}{{0,{extra}}}"
        else:
            body_with_items = f"{item_rule} {tail}{{{min_items - 1},{extra}}}"

        if min_items == 0:
            body = f'"[" ws ({body_with_items} ws)? "]"'
        else:
            body = f'"[" ws {body_with_items} ws "]"'
        return self._add_rule(f"{hint}-arr", body)

    # --------------------------------------------------------------- render
    def build(self) -> str:
        root_rule = self.visit(self.root_schema, "root")
        lines = [f"root ::= {root_rule}"]
        for name, body in self.rules.items():
            lines.append(f"{name} ::= {body}")
        for name in ("string", "number", "integer", "boolean", "null", "ws"):
            if name in self.used_primitives:
                lines.append(PRIMITIVE_RULES[name])
        return "\n".join(lines) + "\n"


def json_schema_to_gbnf(schema: dict[str, Any]) -> str:
    """Compile a JSON Schema into a llama.cpp GBNF grammar."""
    return _GrammarBuilder(schema).build()
