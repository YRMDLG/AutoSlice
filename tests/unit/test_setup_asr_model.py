from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autoslice.setup_asr_model import (
    RECOMMENDED_MODELS,
    _download_model,
    install_recommended_models,
)
from unittest.mock import patch


class SetupAsrModelTests(unittest.TestCase):
    def test_missing_modelscope_reports_optional_install_command(self):
        with patch.dict("sys.modules", {"modelscope": None}):
            with self.assertRaisesRegex(RuntimeError, r'\.\[asr-models\]'):
                install_recommended_models()

    def test_installer_verifies_all_recommended_model_caches(self):
        calls = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_snapshot_download(model_id):
                calls.append(model_id)
                target = root / str(len(calls))
                target.mkdir()
                weight_name = (
                    "campplus_cn_common.bin"
                    if "campplus" in model_id
                    else "model.pt"
                )
                (target / weight_name).write_bytes(b"model")
                return str(target)

            paths = install_recommended_models(snapshot_loader=fake_snapshot_download)

        self.assertEqual(tuple(calls), RECOMMENDED_MODELS)
        self.assertEqual(len(paths), len(RECOMMENDED_MODELS))

    def test_campplus_accepts_official_bin_weight_without_model_pt(self):
        with TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            (model_dir / "campplus_cn_common.bin").write_bytes(b"model")

            resolved = _download_model(
                "iic/speech_campplus_sv_zh-cn_16k-common",
                lambda _model_id: str(model_dir),
                attempts=1,
            )

        self.assertEqual(resolved, model_dir.resolve())


if __name__ == "__main__":
    unittest.main()
