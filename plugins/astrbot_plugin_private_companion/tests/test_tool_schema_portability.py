"""Tool declarations must be constructible by every provider client, not just one.

Google GenAI types ``Schema.enum`` as ``list[str]``. A JSON-Schema-legal integer
enum therefore fails inside that client while the request is still being built:
nothing is sent, every provider on the chain fails the same way, and the whole
conversation class stops answering. These tests keep that class of schema out of
the stored tool definitions.
"""

import json
import unittest

from companion_v2.execution.models import encode
from companion_v2.model_gateway.models import ToolSpec, portable_tool_schema


def has_non_string_enum(node):
    """Report the first non-string enum found anywhere below ``node``."""
    if isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, list) and any(not isinstance(value, str) for value in values):
            return values
        for child in node.values():
            found = has_non_string_enum(child)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = has_non_string_enum(child)
            if found is not None:
                return found
    return None


def assert_gemini_compatible(test, schema, path, *, nested=False):
    if isinstance(schema, bool):
        return
    if not nested:
        test.assertFalse({"anyOf", "oneOf", "allOf"} & schema.keys(), path)
    missing = set(schema.get("required", ())) - schema.get("properties", {}).keys()
    test.assertFalse(missing, f"{path}: required properties missing from this schema: {sorted(missing)}")
    if "enum" in schema:
        values = schema["enum"]
        test.assertIsInstance(values, list, path)
        test.assertTrue(values and all(isinstance(value, str) for value in values),
                        f"{path}: Gemini enum needs nonempty list[str], got {values!r}")
    for keyword in ("properties", "patternProperties", "definitions", "$defs", "dependentSchemas"):
        for name, child in schema.get(keyword, {}).items():
            assert_gemini_compatible(test, child, f"{path}.{keyword}.{name}", nested=True)
    for keyword in ("items", "anyOf", "oneOf", "allOf", "prefixItems", "not", "additionalProperties", "if", "then", "else"):
        if keyword in schema:
            child = schema[keyword]
            if isinstance(child, list):
                for index, item in enumerate(child):
                    assert_gemini_compatible(test, item, f"{path}.{keyword}[{index}]", nested=True)
            else:
                assert_gemini_compatible(test, child, f"{path}.{keyword}", nested=True)


class PortableToolSchemaTests(unittest.TestCase):
    def test_integer_enum_is_dropped_and_its_values_move_into_the_description(self):
        cleaned = portable_tool_schema({
            "type": "object",
            "properties": {
                "row": {"type": "integer", "enum": [0, 1, 2]},
                "mode": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["row"], "additionalProperties": False,
        })

        self.assertEqual(cleaned["properties"]["row"], {"type": "integer", "description": "可选值：0、1、2"})
        # A string enum is exactly what every client can construct, so it stays.
        self.assertEqual(cleaned["properties"]["mode"], {"type": "string", "enum": ["a", "b"]})
        self.assertIsNone(has_non_string_enum(cleaned))

    def test_cleaning_reaches_properties_items_and_every_combinator(self):
        source = {
            "type": "object",
            "properties": {
                "pick": {"type": "array", "items": {"type": "integer", "enum": [1, 2]}},
                "count": {"type": "integer", "minimum": 1},
                "nested": {
                    "anyOf": [{"type": "string", "enum": [3, 4]}],
                    "oneOf": [{"type": "number", "enum": [5]}],
                    "allOf": [{"type": "object", "properties": {"flag": {"type": "integer", "enum": [0, 1]}}}],
                },
            },
        }

        cleaned = portable_tool_schema(source)

        self.assertIsNone(has_non_string_enum(cleaned))
        self.assertEqual(cleaned["properties"]["pick"]["items"]["description"], "可选值：1、2")
        nested = cleaned["properties"]["nested"]
        self.assertEqual(nested["anyOf"][0]["description"], "可选值：3、4")
        self.assertEqual(nested["oneOf"][0]["description"], "可选值：5")
        self.assertEqual(nested["allOf"][0]["properties"]["flag"]["description"], "可选值：0、1")
        self.assertEqual(cleaned["properties"]["count"], {"type": "integer", "minimum": 1})
        # The cleaner works on a copy: the stored business schema is not rewritten.
        self.assertEqual(source["properties"]["pick"]["items"], {"type": "integer", "enum": [1, 2]})

    def test_top_level_combinators_merge_properties_and_keep_only_common_required_fields(self):
        for keyword, requirement in (("anyOf", "至少一组"), ("oneOf", "恰好一组"), ("allOf", "每一组")):
            with self.subTest(keyword=keyword):
                source = {
                    "description": "Choose arguments.",
                    "properties": {"shared": {"type": "string"}},
                    "required": ["shared", "left"],
                    keyword: [
                        {"properties": {"left": {"type": "integer", "enum": [1]}}, "required": ["shared", "left"]},
                        {"properties": {"right": {"type": "boolean"}}, "required": ["shared", "right"]},
                    ],
                }
                cleaned = portable_tool_schema(source)

                self.assertEqual(cleaned, {
                    "type": "object",
                    "description": f"Choose arguments. 必填字段（{requirement}）：shared + left / shared + right。",
                    "properties": {
                        "shared": {"type": "string"},
                        "left": {"type": "integer", "description": "可选值：1"},
                        "right": {"type": "boolean"},
                    },
                    "required": ["shared"],
                })
                self.assertEqual(portable_tool_schema(cleaned), cleaned)
                self.assertEqual(source["properties"], {"shared": {"type": "string"}})

    def test_cleaning_an_already_portable_schema_changes_nothing(self):
        clean = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a"]}},
                 "required": ["mode"], "additionalProperties": False}

        self.assertEqual(portable_tool_schema(clean), clean)
        self.assertEqual(portable_tool_schema(portable_tool_schema(clean)), clean)

    def test_stored_tool_specs_are_cleaned_when_they_are_declared(self):
        spec = ToolSpec("game.move", "Move.", encode({
            "type": "object",
            "properties": {"row": {"type": "integer", "enum": [0, 1, 2]}},
            "required": ["row"], "additionalProperties": False,
        }))

        schema = json.loads(spec.parameters_json)
        self.assertEqual(schema["properties"]["row"], {"type": "integer", "description": "可选值：0、1、2"})

    def test_a_portable_schema_is_stored_byte_for_byte(self):
        clean = encode({"type": "object", "properties": {"mode": {"type": "string", "enum": ["a"]}},
                        "required": ["mode"], "additionalProperties": False})

        self.assertEqual(ToolSpec("device.read", "Read.", clean).parameters_json, clean)


if __name__ == "__main__":
    unittest.main()
