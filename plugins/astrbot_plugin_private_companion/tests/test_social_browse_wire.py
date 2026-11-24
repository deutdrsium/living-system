"""Exercise the installed AstrBot OpenAI query/parser without network I/O."""

import base64
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from openai.types.chat import ChatCompletion

from companion_v2.adapters.astrbot.model_wire import build_wire, decode_response
from companion_v2.model_gateway.models import ToolCall, ToolExchange
from companion_v2.role_work.social import catalog
from tests.native_adapter_support import FakeProvider, NativeFixture


# The supplied abbreviated example fits the old 512-character limit already.
SAMPLE_ID = (
    "call_3427611161158106168__thought__"
    "EpMBCpABAWkUfRNsM299wxo89l4yXUvb1qDhxVbijnr5yra0l2wQHwQ0BK6DoNr9kYoKhjBTZMiW/aRF7bE9aZSEnWj7+5ktupCEFIT0erGECHuB7Xz+b+dzCBMdvuyHKydmgefkcS1PvzCo5I2n6Z2UiUc8jGmCvNsL3v7aim55Nnx/BV5Iw+1Lf7roxjFqpfDSxDBo"
)
THOUGHT_ID = "call_3427611161158106168__thought__" + base64.b64encode(bytes(range(256)) * 5).decode("ascii")


def response(*, tool_id=None, text=None):
    calls = None if tool_id is None else [{"id": tool_id, "type": "function", "function": {
        "name": "social_browse", "arguments": '{"operation":"google_search","query":"stars"}'}}]
    return ChatCompletion.model_validate({"id": "relay-response", "object": "chat.completion", "created": 0,
        "model": "relay-gemini", "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
            "message": {"role": "assistant", "content": text, "tool_calls": calls}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}})


class CoreClient:
    def __init__(self, state=None, max_retries=0):
        self.state = state or NS(calls=[], behavior=None)
        self.max_retries = max_retries
        self.chat = NS(completions=NS(create=self.create))

    def with_options(self, *, max_retries):
        return CoreClient(self.state, max_retries)

    async def create(self, **payload):
        self.state.calls.append(deepcopy(payload))
        return self.state.behavior(payload)


class CoreProvider(FakeProvider, ProviderOpenAIOfficial):
    _query = ProviderOpenAIOfficial._query
    _parse_openai_completion = ProviderOpenAIOfficial._parse_openai_completion

    def __init__(self):
        FakeProvider.__init__(self)
        self.client = CoreClient()
        self.reasoning_key = "reasoning_content"


class SocialBrowseWireTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.native.provider = CoreProvider()
        self.native_model = self.make_model(provider_type=CoreProvider)
        self.native_model.function_tool_type = FunctionTool
        self.native_model.tool_set_type = ToolSet

    async def browse_envelope(self):
        envelope = await self.envelope(tools=catalog({"brightdata": True}))
        return replace(envelope, original=replace(envelope.original, output_format="social_reflection",
                                                 response_format="text"))

    async def test_thought_signature_id_round_trips_through_core_query_and_continuation(self):
        envelope = await self.browse_envelope()
        self.assertLess(len(SAMPLE_ID), 512)
        self.assertEqual(ToolCall(SAMPLE_ID, "social_browse", "{}").call_id, SAMPLE_ID)
        self.assertGreater(len(THOUGHT_ID), 512)
        def respond(payload):
            replies = [message for message in payload["messages"] if message["role"] == "tool"]
            if replies:
                calls = [call for message in payload["messages"] for call in message.get("tool_calls", ())]
                self.assertEqual(calls[0]["id"], THOUGHT_ID)
                self.assertEqual(replies[0]["tool_call_id"], THOUGHT_ID)
                return response(text='{"reflection":"看了搜索结果。"}')
            return response(tool_id=THOUGHT_ID)
        self.native.provider.client.state.behavior = respond
        first = await self.native_model.generate(envelope)
        self.assertEqual(first.status, "generated", first.diagnostics)
        self.assertEqual(first.tool_calls[0].call_id, THOUGHT_ID)
        history = (ToolExchange(first.tool_calls[0], '{"ok":true,"text":"actual search results"}'),)
        final = await self.native_model.generate(replace(envelope, tool_history=history))
        self.assertEqual(final.status, "generated", final.diagnostics)

    async def test_omitted_final_catalog_reproduces_core_empty_output_error(self):
        envelope = await self.browse_envelope()
        history = (ToolExchange(ToolCall(SAMPLE_ID, "social_browse", "{}"), '{"ok":true}'),)
        self.native.provider.client.state.behavior = lambda _: response(tool_id=SAMPLE_ID)
        rejected = await self.native_model.generate(replace(envelope, tools=(), tool_history=history))
        self.assertEqual(rejected.status, "unknown")
        self.assertEqual(rejected.diagnostics.error_type, "EmptyModelOutputError")
        self.assertEqual(rejected.diagnostics.finish_reason, "tool_calls")
        self.assertEqual(rejected.diagnostics.parse_state, "native_rejected")

    async def test_final_turn_keeps_catalog_and_sends_none_through_core_query(self):
        envelope = await self.browse_envelope()
        history = (ToolExchange(ToolCall(SAMPLE_ID, "social_browse", "{}"), '{"ok":true}'),)
        def respond(payload):
            if payload.get("tool_choice") != "none":
                return response(tool_id=SAMPLE_ID)
            self.assertEqual(payload["tools"][0]["function"]["name"], "social_browse")
            self.assertIn("不再调用工具", payload["messages"][-1]["content"])
            return response(text='{"reflection":"浏览结束，记下真实感想。"}')
        self.native.provider.client.state.behavior = respond
        final = await self.native_model.generate(replace(envelope, tool_history=history, allow_tool_calls=False))
        self.assertEqual(final.status, "generated", final.diagnostics)
        self.assertIsNotNone(final.draft)
        self.assertEqual(self.native.provider.client.state.calls[-1]["tool_choice"], "none")

    async def test_thought_ids_remain_bounded_unique_and_host_matched(self):
        envelope = await self.browse_envelope()
        for bad in ("x" * 4097, "control\ncharacter", "非ASCII"):
            with self.subTest(bad_length=len(bad)), self.assertRaises(ValueError):
                ToolCall(bad, "social_browse", "{}")
        self.assertEqual(ToolCall("x" * 4096, "social_browse", "{}").call_id, "x" * 4096)
        wire = build_wire(envelope)
        raw = response(tool_id=THOUGHT_ID)
        parsed = await self.native.provider._parse_openai_completion(raw, ToolSet(tools=[]))
        parsed.tools_call_ids = ["changed"]
        with self.assertRaisesRegex(ValueError, "native tool result differs"):
            decode_response(parsed, wire)
        raw.choices[0].message.tool_calls *= 2
        parsed = await self.native.provider._parse_openai_completion(raw, ToolSet(tools=[]))
        with self.assertRaisesRegex(ValueError, "native tool result differs"):
            decode_response(parsed, wire)
