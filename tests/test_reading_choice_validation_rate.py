"""A waiting reading choice must not re-run full owner validation on every runner poll."""
import asyncio
import json
import unittest
from unittest.mock import patch

from test_reading_extraction import ReadingExtractionFixture
from tests.test_role_tool_loop import answered
from companion_v2.execution.models import encode
from companion_v2.model_gateway.life_tasks import LifeModelTasks
from good_life.reading_selection import choose

MODEL_SECONDS = 3
# submit, claim, begin, routing, model rounds, effect and finish each
# revalidate the owner inputs once (14 in this fixture).
BOUNDARY_CHECKS = 14


class ReadingChoiceValidationRateTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_waiting_choice_revalidates_inputs_about_once_per_second(self):
        f = await self.fixture(start=False)
        calls = []
        validate = LifeModelTasks.validate

        def counted(tasks, *args, **kwargs):
            calls.append(1)
            return validate(tasks, *args, **kwargs)

        self.model_behavior = lambda _env: asyncio.sleep(
            MODEL_SECONDS, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(LifeModelTasks, "validate", counted):
            result = await choose(f.executor, f.context.key, [json.loads(encode(f.activity))], "rate", "window")
        self.assertEqual(result["state"], "committed", result)
        # Revalidating on every 50 ms runner poll gave ~70 here and kept a core
        # busy re-digesting every candidate book while the model was waiting.
        self.assertLessEqual(len(calls), BOUNDARY_CHECKS + MODEL_SECONDS + 1)


if __name__ == "__main__":
    unittest.main()
