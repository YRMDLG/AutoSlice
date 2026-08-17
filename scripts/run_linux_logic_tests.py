"""运行 Linux 纯逻辑白名单测试；不代表 Linux 完整媒体支持。"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.external_boundary_guard import (
    install_test_external_boundary_guard,
)

# 仅列入不需要 FFmpeg、字体、模型、GPU、Flask 服务、真实 LLM/网络、
# 私人配置或用户媒体的测试。这里刻意不使用 discover，避免新增重测试被
# 无意纳入，也明确排除人工调试入口 test_engine.py。
LINUX_LOGIC_TEST_TARGETS = (
    "tests.architecture.test_architecture_contracts",
    "tests.architecture.test_external_boundaries",
    "tests.unit.test_media_formats",
    "tests.integration.test_topic_engine.DanmakuAnalysisTests",
    "tests.integration.test_topic_engine.DanmakuPeakScoringTests",
    "tests.integration.test_topic_engine.CandidateReviewTests",
    "tests.integration.test_topic_engine.TitleReviewTests",
    "tests.integration.test_topic_engine.TitleStyleEvidenceTests",
    "tests.integration.test_topic_engine.ManualTimelineTests",
    "tests.unit.test_task_store",
    "tests.unit.test_task_registry",
    "autocover_tool.tests.test_titles",
)

PRIVATE_ENVIRONMENT_NAMES = (
    "AUTOSLICE_ANALYSIS_MODEL",
    "AUTOSLICE_ANALYSIS_REASONING_EFFORT",
    "AUTOSLICE_API_BASE_URL",
    "AUTOSLICE_API_TOKEN",
    "AUTOSLICE_API_TYPE",
    "AUTOSLICE_LLM_MODEL",
    "AUTOSLICE_LLM_PROXY_HTTP",
    "AUTOSLICE_LLM_PROXY_HTTPS",
    "AUTOSLICE_LLM_PROXY_MODE",
    "AUTOSLICE_LLM_REVIEW_MODEL",
    "AUTOSLICE_REVIEW_REASONING_EFFORT",
    "AUTOSLICE_STREAMER_PROFILES",
    "AUTOSLICE_TITLE_STYLE_PROFILE",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--quiet", action="store_true", help="减少 unittest 输出")
    args = parser.parse_args(argv)

    with TemporaryDirectory(prefix="autoslice-linux-logic-") as directory:
        isolated_root = Path(directory)
        environment = {
            "AUTOSLICE_LOCAL_CONFIG": str(isolated_root / "autoslice.local.json"),
            "AUTOSLICE_TASK_DB": str(isolated_root / "tasks.sqlite3"),
            "AUTOSLICE_OUTPUT_DIR": str(isolated_root / "output"),
            "AUTOSLICE_TIMELINE_DIR": str(isolated_root / "timelines"),
            "AUTOSLICE_TITLE_STYLE_PROFILE": str(
                ROOT / "title_style_profile.example.json"
            ),
        }
        managed_names = (*environment, *PRIVATE_ENVIRONMENT_NAMES)
        previous = {name: os.environ.get(name) for name in managed_names}
        for name in PRIVATE_ENVIRONMENT_NAMES:
            os.environ.pop(name, None)
        os.environ.update(environment)
        try:
            install_test_external_boundary_guard(linux_logic_only=True)
            suite = unittest.defaultTestLoader.loadTestsFromNames(
                LINUX_LOGIC_TEST_TARGETS
            )
            result = unittest.TextTestRunner(
                verbosity=0 if args.quiet else 2
            ).run(suite)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
