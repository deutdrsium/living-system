from tempfile import TemporaryDirectory
import unittest

from test_state_model_support import ROOT, StateAccess, parameters
from good_life.runtime import LifeRuntime
from good_life.state_model import ShadowConfig
from good_life.storage import LifeStorage


class ShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_disabled_runtime_have_no_shadow_schema_or_handler_changes(self):
        with TemporaryDirectory(prefix=".state-model-test-", dir=ROOT) as directory:
            storage = LifeStorage.open(directory)
            try:
                default = LifeRuntime(storage, object())
                handlers = set(default.handlers)
                self.assertIsNone(default.state_model)
                self.assertIsNone(storage._connection.execute(
                    "SELECT value FROM life_metadata WHERE key='state_model_schema_version'").fetchone())
                await default.close()
                disabled = LifeRuntime(storage, object(), state_model_config=ShadowConfig())
                self.assertIsNone(disabled.state_model)
                self.assertEqual(set(disabled.handlers), handlers)
                self.assertIsNone(storage._connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='life_state_model_heads'").fetchone())
                await disabled.close()
                enabled = LifeRuntime(storage, object(), state_model_config=ShadowConfig(
                    enabled=True, sleep=True, hunger=True, energy=True, parameters=parameters()), state_model_access=StateAccess())
                self.assertEqual(set(enabled.handlers), handlers)
                self.assertIsNone(enabled.scheduler)
                self.assertIsNotNone(enabled.state_model)
                self.assertEqual(storage._connection.execute("SELECT COUNT(*) FROM life_state_model_heads").fetchone()[0], 0)
                await enabled.close()
                self.assertTrue(enabled.state_model.closed)
            finally:
                storage.close()
