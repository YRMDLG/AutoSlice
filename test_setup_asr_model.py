from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setup_asr_model import RECOMMENDED_MODELS, install_recommended_models


class SetupAsrModelTests(unittest.TestCase):
    def test_installer_verifies_all_recommended_model_caches(self):
        calls = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_snapshot_download(model_id):
                calls.append(model_id)
                target = root / str(len(calls))
                target.mkdir()
                (target / "model.pt").write_bytes(b"model")
                return str(target)

            paths = install_recommended_models(snapshot_loader=fake_snapshot_download)

        self.assertEqual(tuple(calls), RECOMMENDED_MODELS)
        self.assertEqual(len(paths), len(RECOMMENDED_MODELS))


if __name__ == "__main__":
    unittest.main()
