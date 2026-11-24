"""Native candidate intake must classify before core generation or sending."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.config import DEFAULT_CONFIG
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from companion_v2.conversation.reply_settings import (ReplyConfigurationOwner, configuration_fields,
                                                      retire_private_closing_values, target_for, validate)
from companion_v2.conversation.participation import (CONFIG_GROUP, GroupParticipationConfigurationOwner,
                                                    configuration_fields as participation_fields)
from companion_v2.decision.client import LayaClientError
from astrbot_host.laya_client import LayaDecisionClient
from companion_v2.decision.contract import CONFIGURABLE_DECISION_FIELDS
from companion_v2.decision.prompt import build_state, decision_questions, render_model_state
from companion_v2.decision.service import LayaDecisionService
from companion_v2.decision.store import LayaSettingsStore, LayaSettingsError
from companion_v2.foundation.outcome import Outcome
from companion_v2.model_gateway.core_observations import observations_for_role
from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.core_reply_decision import _private_silence, _recent_private_messages, core_decision_facts
from companion_v2.configuration.models import encode
from tests.group_host_support import GroupFixture, group_config
from tests.test_laya_decision import laya_result
from tests.test_main_no_reply import ENTRY
from tests.test_core_reply_pipeline import PipelineEvent, CoreAgentFixture


class CoreDecisionPromptTests(unittest.TestCase):
    def test_followup_decision_respects_closed_interjection_and_encourages_natural_closure(self):
        questions = decision_questions({"scope_kind": "group", "candidate_reason": "reply_follow_up",
            "group_participation_policy": {"question_enabled": False, "interjection_enabled": False},
            "group_continuation": {"used_turns": 2, "max_turns": 3}}, {})
        action = questions["action"]
        self.assertIn("答疑", action["instructions"])
        self.assertIn("上限", action["instructions"])
        self.assertIn("逐句追答", action["criteria"]["silent"])
        self.assertNotIn("interject", action["criteria"])

    def test_group_listening_criteria_do_not_change_private_questions(self):
        enabled = dict.fromkeys(CONFIGURABLE_DECISION_FIELDS, True)
        group = decision_questions({"scope_kind": "group"}, enabled)
        private = decision_questions({"scope_kind": "private"}, enabled)
        self.assertIn("未叫机器人时旁听", group["action"]["criteria"]["silent"])
        self.assertIn("候选", group["action"]["instructions"])
        self.assertNotIn("旁听", private["action"]["criteria"]["silent"])
        self.assertNotIn("interject", private["action"]["criteria"])

    def test_private_question_asks_whether_the_conversation_has_come_to_rest(self):
        from companion_v2.decision.prompt import PRIVATE_ACTION_QUESTION
        private = decision_questions({"scope_kind": "private"}, dict.fromkeys(CONFIGURABLE_DECISION_FIELDS, True))
        self.assertEqual(private["action"], PRIVATE_ACTION_QUESTION)
        self.assertTrue(private["action"]["instructions"].startswith("当前消息是唯一待判断发言。这是私聊。"))
        self.assertNotIn("历史只用于", private["action"]["instructions"])
        self.assertEqual(set(private["action"]["criteria"]), {"reply", "silent", "wait", "clarify"})
        self.assertEqual(private["turn_state"], decision_questions({"scope_kind": "unknown"}, {})["turn_state"])

    def test_private_state_renders_recent_dialogue_with_the_roles_proactive_last_word(self):
        facts = {"scope_kind": "private", "user_text": "那就明天见\n晚安",
                 "current_at": "2025-01-15T14:05:00+00:00", "recent_private_messages": [
            {"role": "user", "text": "今天的展览结束了", "at": "2025-01-15T13:00:00+00:00"},
            {"role": "assistant", "text": "明早再聊展览，晚安呀", "at": "2025-01-15T14:00:00+00:00",
             "proactive": True}]}
        model = json.loads(render_model_state(build_state(facts)))["facts"]
        self.assertEqual(model["conversation_basis"], "validated_private_native_dialogue")
        self.assertTrue(model["assistant_reply_returned"])
        self.assertEqual(model["conversation"][-1], {"speaker": "assistant", "at": "2025-01-15T14:00:00+00:00",
                                                     "message": "明早再聊展览，晚安呀", "proactive": True})
        self.assertEqual(model["user_text"], "那就明天见\n晚安")
        self.assertEqual(model["current_at"], "2025-01-15T14:05:00+00:00")

    def test_private_history_merges_proactive_contacts_by_time(self):
        class Dialogue:
            def __init__(self, _history):
                pass

            def capture(self, *_args, **_kwargs):
                return {}

            def read(self, *_args, **_kwargs):
                return ({"role": "user", "text": "今天的展览结束了", "received_at": "2025-01-15T13:00:00+00:00"},)

        background = NS(capture=lambda *_args, **_kwargs: {}, read=lambda *_args, **_kwargs: (
            {"text": "明早再聊展览，晚安呀", "delivered_at": "2025-01-15T14:00:00+00:00"},))
        with patch("companion_v2.conversation.private_history.PrivateConversationHistory", Dialogue):
            recent = _recent_private_messages(NS(background_history=background), object(), "event", None)
        self.assertEqual([item["text"] for item in recent], ["今天的展览结束了", "明早再聊展览，晚安呀"])
        self.assertTrue(recent[-1]["proactive"])
        self.assertNotIn("proactive", recent[0])


class CoreReplyDecisionTests(GroupFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = self.make_controller(group_config(group_delivery_mode="onebot_group"))
        self.app = await self.start_host()
        self.native_laya_resolver = self.app.laya_decision_resolver
        self.plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        self.plugin.staging = self.controller
        self.fields = dict.fromkeys(CONFIGURABLE_DECISION_FIELDS, True)
        self.enabled, self.prediction, self.calls = True, laya_result(), []
        self.private_enabled = True

        def resolver(context):
            store = LayaSettingsStore(self.root / "decision-config", context.scope.native_scope_id,
                                      context.role_binding.role.subject_id)
            store.update(store.raw()["revision"], {"enabled": self.enabled, "decision_fields": self.fields,
                                                   "private_silence_enabled": self.private_enabled})
            return LayaDecisionService(store, runtime=self.app.runtime, origin_key=context.key, client_factory=LayaDecisionClient)

        self.app.attach_laya_decision(resolver)

        def predict(state, questions):
            self.calls.append((json.loads(state)["facts"], deepcopy(questions)))
            if isinstance(self.prediction, Exception):
                raise self.prediction
            return self.prediction

        cuda_available = patch("astrbot_host.laya_client._cuda_available", return_value=True)
        cuda_available.start()
        self.addCleanup(cuda_available.stop)
        mocked = self.predict_patch = patch("astrbot_host.laya_client._local_predict", side_effect=predict)
        mocked.start()
        self.addCleanup(mocked.stop)
        context = (await self.controller.observe_group_early(self.group_event(identifier="configure-decision"))).unwrap()
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        owner, target = ReplyConfigurationOwner(self.app.runtime), target_for(context)
        owner.write(target, fields, changes={"conversation.presentation.group_wakeup_question_enabled": True},
                    reset=(), expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())

    async def flush(self):
        observer = self.controller.session.core_reply_observations
        while observer._pending:
            await asyncio.gather(*tuple(observer._pending), return_exceptions=False)

    def incoming(self, identifier, text, **options):
        event = self.group_event(identifier=identifier, text=text, **options)
        event.is_wake = event.is_at_or_wake_command = False
        return event

    async def intake(self, event):
        await self.plugin.receive_companion_message(event)
        await self.flush()
        return event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]

    def assert_no_provider_or_send(self, event):
        self.assertEqual(event.sent, [])
        self.assertEqual(self.native.provider.client.state.calls, [])
        self.assertEqual(self.app.runtime.read_all("SELECT * FROM conversation_batches", ()), ())

    async def test_merged_group_candidate_keeps_original_fragment_ids(self):
        participation = self.app.tools.group_participation
        event = self.incoming("merged-latest", "小星\n帮我看看")
        with (patch.object(participation, "observe_early", wraps=participation.observe_early) as observe,
              patch.object(participation.admission, "candidate",
                           wraps=participation.admission.candidate) as candidate):
            result = await self.controller.group_reply_candidate(
                event, merged_event_ids=("merged-first",), latest_native_text="帮我看看")
        self.assertTrue(result.is_success, result.failure)
        self.assertEqual(observe.call_args.kwargs["text"], "帮我看看")
        self.assertEqual(candidate.call_args.kwargs["merged_event_ids"], ("merged-first",))

    async def test_silent_candidate_never_wakes_core_or_sends_and_logs_laya_source(self):
        self.prediction = laya_result({"action": "silent"})
        event = self.incoming("other-member-question", "dsh 有没有什么好玩的插件")
        with self.assertLogs(ENTRY.prepare_core_reply_tracking.__globals__["logger"], level="INFO") as captured:
            prepared = await self.intake(event)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(event.call_llm)
        self.assertFalse(event.is_at_or_wake_command)
        self.assert_no_provider_or_send(event)
        self.assertIn("reason_source=laya", "\n".join(captured.output))
        self.assertIn("action=silent", "\n".join(captured.output))
        rows = self.app.runtime.read_all("SELECT * FROM laya_decision_events WHERE batch_id = ?", (prepared["turn_id"],))
        self.assertEqual(rows[0]["status"], "ok")
        record = observations_for_role(self.app.runtime, prepared["context"])[0]
        self.assertEqual(record["generation_state"], "suppressed")
        self.assertEqual(record["requests"], [])
        self.assertEqual(record["delivery"]["attempts"], 0)

    async def test_wait_keeps_follow_up_candidate_for_same_user_and_is_not_a_sent_reply(self):
        self.prediction = laya_result({"action": "wait", "turn_state": "unfinished"})
        first = self.incoming("unfinished-question", "可以帮我看看吗？我还没说完")
        prepared = await self.intake(first)
        self.assertTrue(first.call_llm)
        self.assert_no_provider_or_send(first)
        self.clock.advance(timedelta(seconds=1))
        second = self.incoming("question-continuation", "就是选哪一款更合适")
        candidate = (await self.controller.group_reply_candidate(second)).unwrap()
        self.assertTrue(candidate["admitted"])
        self.assertEqual(candidate["reason"], "active_follow_up")
        self.prediction = laya_result({"action": "reply"})
        await self.intake(second)
        self.assertTrue(second.is_at_or_wake_command)
        self.assertTrue(any("还没说完" in item["text"] for item in self.calls[-1][0]["recent_group_messages"]))
        saved = self.app.tools.group_participation.admission.recorded_core_reply(prepared["context"], prepared["event_id"])
        self.assertEqual(saved["participation"]["decision_action"], "wait")
        laya_row = self.app.runtime.read_one("SELECT processing_json FROM laya_decision_events WHERE batch_id = ?",
                                             (prepared["turn_id"],))
        processing = json.loads(laya_row["processing_json"])
        self.assertEqual(processing["application"]["applied_decision"]["action"], "wait")
        self.assertTrue(processing["application"]["suppress_reply"])

    async def test_reply_interject_and_clarify_reach_core_with_enabled_guidance(self):
        for action in ("reply", "interject", "clarify"):
            with self.subTest(action=action):
                self.prediction = laya_result({"action": action, "reply_style": "brief", "segment_mode": "keep_together"})
                event = self.incoming("core-advice-" + action, "有没有什么好玩的插件？")
                prepared = await self.intake(event)
                self.assertTrue(event.is_at_or_wake_command)
                self.assertFalse(getattr(event, "call_llm", False))
                req = NS(system_prompt="AstrBot 人格", extra_user_content_parts=[])
                await self.plugin.prepare_companion_core_request(event, req)
                context_text = "\n".join(
                    part.text for part in req.extra_user_content_parts
                    if isinstance(getattr(part, "text", None), str))
                self.assertIn("Laya 本轮语义判断", context_text)
                self.assertIn("步骤、列表、引用或代码应保持完整", context_text)
                self.assertNotIn("Laya 本轮语义判断", req.system_prompt)
                self.assertEqual(prepared["laya_decision"]["applied_decision"]["action"], action)
                self.assert_no_provider_or_send(event)

    async def test_disabled_action_does_not_apply_neutral_reply_default_or_silence(self):
        self.fields["action"] = self.fields["segment_mode"] = False
        self.prediction = laya_result({"action": "silent", "segment_mode": "single"})
        event = self.incoming("action-disabled", "有没有好玩的插件？")
        prepared = await self.intake(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertNotIn("action", self.calls[0][1])
        self.assertNotIn("action", prepared["laya_decision"]["applied_decision"])
        self.assertNotIn("不必拆成多条", prepared["prompt_instruction"])

    async def test_disabled_laya_and_all_disabled_fields_skip_inference(self):
        for name, enabled, fields in (("master", False, self.fields),
                                     ("all-fields", True, dict.fromkeys(self.fields, False))):
            self.enabled, self.fields = enabled, fields
            event = self.incoming(name, "有没有好玩的插件？")
            prepared = await self.intake(event)
            self.assertEqual(prepared["laya_decision"]["status"], "skipped")
            self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(self.calls, [])

    async def test_robot_quote_is_a_native_rule_skip_and_remains_replyable(self):
        event = self.incoming("quoted-bot", "继续说说")
        event.message_obj.message.insert(0, NS(type="Reply", id="bot-message", sender_id="100"))
        prepared = await self.intake(event)
        self.assertEqual(self.calls, [])
        self.assertEqual(prepared["laya_decision"]["reason"], "robot_message_reference")
        self.assertTrue(event.is_at_or_wake_command)

    async def test_inference_failure_is_recorded_and_falls_back_to_core(self):
        self.prediction = LayaClientError("laya_inference_failed", "private error text")
        event = self.incoming("decision-failure", "有没有好玩的插件？")
        with self.assertLogs(ENTRY.prepare_core_reply_decision.__globals__["logger"], level="WARNING") as captured:
            prepared = await self.intake(event)
        self.assertNotIn("private error text", "\n".join(captured.output))
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(prepared["laya_decision"]["status"], "failed")
        row = self.app.runtime.read_one("SELECT * FROM laya_decision_events WHERE batch_id = ?", (prepared["turn_id"],))
        self.assertEqual(row["status"], "failed")

    async def test_settings_rejection_logs_the_real_reason_beside_its_code(self):
        """A refused configuration must say which value was wrong, not only its code."""
        reason = "mode 必须是 local、remote、jev、workers_ai"

        class Rejected:
            def set_trace(self, **_kwargs):
                pass

            async def decide(self, _facts):
                raise LayaSettingsError("laya_config_invalid", reason)

        self.app.attach_laya_decision(lambda _context: Rejected())
        event = self.incoming("settings-rejected", "有没有好玩的插件？")
        with self.assertLogs(ENTRY.prepare_core_reply_decision.__globals__["logger"], level="WARNING") as captured:
            prepared = await self.intake(event)
        output = "\n".join(captured.output)
        self.assertIn("reason=laya_config_invalid", output)
        self.assertIn("workers_ai", output)
        self.assertEqual(prepared["laya_decision"]["reason"], "laya_config_invalid")
        self.assertEqual(prepared["laya_decision"]["reason_detail"], reason)
        self.assertTrue(event.is_at_or_wake_command)

    async def test_group_history_uses_authorized_window_and_current_native_address_facts(self):
        await self.controller.observe_group_early(self.incoming("other-group-history", "别群私密内容", target="group-b"))
        await self.controller.observe_group_early(self.incoming("same-group-history", "这是本群当前讨论"))
        event = self.incoming("addressed-question", "有没有好玩的插件？")
        event.message_obj.message.insert(0, NS(type="At", qq="100"))
        prepared = await self.intake(event)
        self.assertEqual(self.calls, [])
        self.assertTrue(prepared["group_admission"]["mentioned"])
        facts = core_decision_facts(event, prepared, self.app, at=self.clock.now())
        self.assertTrue(facts["mentioned"])
        bodies = [item["text"] for item in facts["recent_group_messages"]]
        self.assertIn("这是本群当前讨论", bodies)
        self.assertNotIn("别群私密内容", bodies)
        self.assertNotIn("有没有好玩的插件？", bodies)

    async def test_explicit_at_mention_bypasses_laya_silence(self):
        self.prediction = laya_result({"action": "silent"})
        event = self.incoming("explicit-at", "怎么让 Laya 必回 @")
        event.message_obj.message.insert(0, NS(type="At", qq="100"))
        prepared = await self.intake(event)
        self.assertEqual(self.calls, [])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(prepared["laya_decision"]["suppress_reply"])
        self.assertEqual(prepared["laya_decision"]["status"], "skipped")
        self.assertEqual(prepared["laya_decision"]["reason"], "explicit_mention")
        self.assertEqual(prepared["laya_decision"]["applied_decision"], {"action": "reply"})
        self.assert_no_provider_or_send(event)

    async def test_group_decision_reads_recent_bot_question_from_send_return_history(self):
        target = self.controller.settings.target("group-a")
        prior_event = self.incoming("group-context-prior", "周末想找个地方吃饭")
        await self.controller.observe_group_early(prior_event)
        prior = (await self.controller.prepare_core_reply(prior_event, target)).unwrap()
        await self.flush()
        self.clock.advance(timedelta(seconds=1))
        self.controller.session.core_reply_observations.output(
            prior, "你更想吃火锅，还是找安静一点的餐馆？", state="send_returned")
        await self.flush()

        self.clock.advance(timedelta(seconds=1))
        current = self.incoming("group-context-current", "安静点的")
        prepared = (await self.controller.prepare_core_reply(current, target)).unwrap()
        progress = {"used_turns": 2, "max_turns": 3}
        prepared.update(channel="group", group_admission={"reason": "reply_follow_up", "continuation": progress})
        facts = core_decision_facts(current, prepared, self.app, at=self.clock.now())
        self.assertEqual(facts["user_text"], "安静点的")
        self.assertEqual(facts["group_continuation"], progress)
        self.assertTrue(any("火锅" in item["text"] for item in facts["recent_group_bot_messages"]))

    async def test_repeat_participation_is_classified_before_literal_action_without_main_model(self):
        from companion_v2.configuration.models import ConfigLayer, ConfigTarget

        context = (await self.controller.observe_group_early(self.incoming("repeat-settings", "群聊讨论"))).unwrap()
        owner, fields = GroupParticipationConfigurationOwner(self.app.runtime), participation_fields()
        target = ConfigTarget(context.scope.installation_id, ConfigLayer.WINDOW, context.role_binding.role, context.scope)
        owner.write(target, fields, changes={CONFIG_GROUP + ".enable_group_interjection": True}, reset=(),
                    expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        for index in range(3):
            self.clock.advance(timedelta(seconds=1))
            candidate = (await self.controller.group_reply_candidate(
                self.incoming("repeat-observed-" + str(index), "好", sender=str(210 + index)))).unwrap()
            self.assertFalse(candidate["admitted"])
        self.clock.advance(timedelta(seconds=1))
        self.prediction = laya_result({"action": "silent"})
        suppressed = self.incoming("repeat-silent", "好", sender="214")
        prepared = await self.intake(suppressed)
        self.assertEqual(prepared["group_admission"]["reason"], "repeat")
        self.assertEqual(self.calls[-1][0]["candidate_reason"], "repeat")
        self.assert_no_provider_or_send(suppressed)
        self.prediction = laya_result({"action": "interject"})
        self.clock.advance(timedelta(seconds=1))
        permitted = self.incoming("repeat-permitted", "好", sender="215")
        literal = AsyncMock(return_value=Outcome.success({"action": "follow", "state": "skipped"}))
        with patch.object(self.controller, "receive_group_repeat", literal):
            await self.plugin.receive_companion_message(permitted)
        literal.assert_awaited_once()
        self.assertEqual(literal.await_args.kwargs["prepared"]["laya_decision"]["applied_decision"]["action"], "interject")
        self.assertTrue(permitted.call_llm)
        self.assert_no_provider_or_send(permitted)

    @staticmethod
    def private_prediction(action, confidence):
        payload = laya_result({"action": action})
        payload["answers"]["action"]["answer_confidence"] = confidence
        return payload

    def no_reply_reasons(self):
        return [row["reason_code"] for row in self.app.runtime.read_all(
            "SELECT reason_code FROM inbound_no_replies WHERE channel = 'private'", ())]

    async def test_confident_private_silence_skips_the_model_and_records_its_reason(self):
        self.prediction = self.private_prediction("silent", 0.92)
        event = self.event(identifier="private-goodnight", text="那就明天见\n晚安")
        with self.assertLogs(ENTRY.prepare_core_reply_decision.__globals__["logger"], level="INFO") as captured:
            prepared = await self.intake(event)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["scope_kind"], "private")
        self.assertIn("recent_private_messages", self.calls[0][0])
        self.assertIn("判断一个真人此刻是否还会回这条消息", self.calls[0][1]["action"]["instructions"])
        self.assertEqual(self.calls[0][0]["current_at"], prepared["native_input"].at.isoformat())
        decision = prepared["laya_decision"]
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "laya_private_silent")
        self.assertEqual(decision["private_silence"]["status"], "passed")
        self.assertFalse(getattr(event, "is_at_or_wake_command", False))
        self.assert_no_provider_or_send(event)
        self.assertEqual(self.no_reply_reasons(), ["laya_private_silent"])
        self.assertIn("channel=private", "\n".join(captured.output))
        record = observations_for_role(self.app.runtime, prepared["context"])[0]
        self.assertEqual(record["generation_state"], "suppressed")
        row = self.app.runtime.read_one("SELECT status FROM laya_decision_events WHERE batch_id = ?",
                                        (prepared["turn_id"],))
        self.assertEqual(row["status"], "ok")

    async def test_private_silence_holds_proactive_until_the_user_speaks_again(self):
        from companion_v2.diagnostics.inbound import turn_token
        from companion_v2.proactive.rhythm import ContactRhythm

        rhythm = ContactRhythm(self.app.runtime)
        self.prediction = self.private_prediction("silent", 0.92)
        prepared = await self.intake(self.event(identifier="private-rest", text="晚安"))
        context = prepared["context"]
        row = self.app.runtime.read_one(
            "SELECT origin_key, code FROM inbound_no_replies WHERE reason_code = 'laya_private_silent'", ())
        self.assertEqual(row["code"], turn_token(prepared["turn_id"]))
        self.assertEqual(row["origin_key"], context.key)
        interaction = self.app.background_history.interaction(context, self.clock.now())
        self.assertIsNotNone(rhythm.private_silence_at(context, interaction))

        self.clock.advance(timedelta(minutes=3))
        self.prediction = self.private_prediction("reply", 0.9)
        later = await self.intake(self.event(identifier="private-again", text="对了，展馆几点开门？"))
        self.assertFalse(later["laya_decision"]["suppress_reply"])
        interaction = self.app.background_history.interaction(context, self.clock.now())
        self.assertIsNone(rhythm.private_silence_at(context, interaction))

    async def test_private_low_confidence_silence_replies(self):
        self.prediction = self.private_prediction("silent", 0.55)
        event = self.event(identifier="private-weak", text="晚安")
        prepared = await self.intake(event)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(prepared["laya_decision"]["suppress_reply"])
        self.assertEqual(prepared["laya_decision"]["reason"], "laya_private_silence_below_threshold")
        self.assertTrue(event.is_at_or_wake_command)
        self.assertNotIn("Laya 本轮语义判断", prepared.get("prompt_instruction", ""))
        self.assertEqual(self.no_reply_reasons(), [])

    def test_only_a_confident_private_silent_suppresses(self):
        def gate(action, confidence, status="ok"):
            return _private_silence({"status": status, "action_confidence": confidence}, {"action": action}, 80)[0]

        self.assertTrue(gate("silent", 0.80))
        self.assertFalse(gate("silent", 0.79))
        self.assertFalse(gate("silent", None))
        for action in ("reply", "wait", "clarify", "interject"):
            self.assertFalse(gate(action, 0.99), action)
        self.assertFalse(gate("silent", 0.99, status="failed"))

    async def test_private_laya_failure_replies(self):
        self.prediction = LayaClientError("laya_inference_timeout", "private error text")
        event = self.event(identifier="private-failure", text="晚安")
        prepared = await self.intake(event)
        self.assertEqual(prepared["laya_decision"]["status"], "failed")
        self.assertFalse(prepared["laya_decision"]["suppress_reply"])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(self.no_reply_reasons(), [])

    async def test_private_switch_off_restores_always_reply_without_inference(self):
        self.private_enabled = False
        self.prediction = self.private_prediction("silent", 0.99)
        event = self.event(identifier="private-switch-off", text="晚安")
        prepared = await self.intake(event)
        self.assertEqual(self.calls, [])
        self.assertEqual(prepared["laya_decision"]["reason"], "laya_private_disabled")
        self.assertTrue(event.is_at_or_wake_command)
        # Groups keep using Laya when only the private switch is off.
        self.prediction = laya_result({"action": "silent"})
        await self.intake(self.incoming("group-still-decides", "dsh 有没有什么好玩的插件"))
        self.assertEqual(len(self.calls), 1)

    async def test_retired_closing_guard_values_load_and_are_removed(self):
        context = (await self.controller.observe_group_early(self.group_event(identifier="retired-values"))).unwrap()
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        owner = ReplyConfigurationOwner(self.app.runtime)
        target = owner._stable_group_target(target_for(context))
        store = self.app.runtime.configuration_store
        with store.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO configuration_targets VALUES (?, ?, 0, ?)",
                               (target.key, encode(target.document()), "2026-09-21T00:00:00.000000+00:00"))
            for name, value in (("enabled", "true"), ("window_seconds", "600"), ("prompt_enabled", "false")):
                connection.execute("INSERT INTO configuration_values VALUES (?, ?, ?, 1)",
                    (target.key, "conversation.presentation.private_closing_guard_" + name, value))
        snapshot = owner.read(target, fields)
        self.assertFalse(any("private_closing" in key for key in snapshot.fields))
        validate({key.removeprefix("conversation.presentation."): item.value for key, item in snapshot.fields.items()})
        revision = snapshot.revision
        self.assertEqual(retire_private_closing_values(self.app.runtime), 3)
        self.assertEqual(retire_private_closing_values(self.app.runtime), 0)
        self.assertGreater(owner.read(target, fields).revision, revision)
        self.assertEqual(store.read_all("SELECT 1 FROM configuration_values WHERE field_id LIKE ?",
                                        ("%private_closing_guard%",)), ())

    async def test_native_private_and_group_resolvers_read_same_global_configuration(self):
        private = (await self.controller.prepare_core_reply(self.event(identifier="global-private"))).unwrap()
        group = (await self.controller.prepare_core_reply(self.incoming("global-group", "群聊天"))).unwrap()
        first = self.native_laya_resolver(private["context"])
        second = self.native_laya_resolver(group["context"])
        self.assertEqual(first.store.path, second.store.path)
        self.assertNotEqual(first.origin_key, second.origin_key)
        first.save(first.status()["revision"], {"enabled": True, "decision_fields": {"action": False}})
        self.assertTrue(second.status()["enabled"])
        self.assertFalse(second.status()["decision_fields"]["action"])


class RealCoreDecisionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_default_path_starts_native_process_agent_without_laya(self):
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        event, target = PipelineEvent(), NS(target_id="private-target")
        plugin.staging = NS(claims=lambda _: True, target_for_event=lambda _: target,
            persona_mapping_for_event=AsyncMock(return_value=NS(role_id="persona")),
            session_topology=NS(reply_enabled=AsyncMock(return_value=True), schedule_observe=lambda _: None),
            prepare_core_reply=AsyncMock(return_value=Outcome.success({"event_id": "silence", "turn_id": "core-silence"})),
            authorize_core_reply=AsyncMock(return_value=Outcome.success(True)),
            session=NS(application=None),
            runtime=None, now=lambda: None)
        event.is_at_or_wake_command = True
        await plugin.receive_companion_message(event)
        stage = ProcessStage()
        stage.ctx = NS(astrbot_config=deepcopy(DEFAULT_CONFIG))
        agent = stage.agent_sub_stage = CoreAgentFixture(plugin)
        async for _ in stage.process(event):
            pass
        self.assertEqual(len(agent.requests), 1)
        self.assertEqual(event.sent, [])


if __name__ == "__main__":
    unittest.main()
