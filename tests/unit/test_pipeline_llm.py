import unittest

from autoslice.pipeline_llm import analyze_pipeline_llm_chunks


class PipelineLlmTests(unittest.TestCase):
    def test_migrates_legacy_checkpoint_before_delegating_to_llm_owner(self):
        calls = []
        chunks = [{"start": 0, "end": 60, "text": "字幕"}]
        expected = ([{"title": "话题"}], [2], "分块失败")

        def seed(destination, legacy):
            calls.append(("seed", destination, legacy))

        def analyze(received_chunks, streamer_name, **kwargs):
            calls.append(("analyze", received_chunks, streamer_name, kwargs))
            return expected

        result = analyze_pipeline_llm_chunks(
            chunks,
            "测试主播",
            "bundle/topic-checkpoint.json",
            "recording_topic_analysis_checkpoint.json",
            progress_callback="progress",
            seed_artifact_from_legacy=seed,
            analyze_topic_chunks=analyze,
        )

        self.assertIs(result, expected)
        self.assertEqual(
            calls,
            [
                (
                    "seed",
                    "bundle/topic-checkpoint.json",
                    "recording_topic_analysis_checkpoint.json",
                ),
                (
                    "analyze",
                    chunks,
                    "测试主播",
                    {
                        "progress_callback": "progress",
                        "checkpoint_path": "bundle/topic-checkpoint.json",
                    },
                ),
            ],
        )

    def test_propagates_llm_result_and_failures_without_interpreting_them(self):
        expected = ([], [1], "LLM 暂时不可用")
        seen = []

        def analyze(*args, **kwargs):
            seen.append((args, kwargs))
            return expected

        result = analyze_pipeline_llm_chunks(
            [],
            "测试主播",
            "checkpoint.json",
            "legacy.json",
            seed_artifact_from_legacy=lambda *_args: None,
            analyze_topic_chunks=analyze,
        )

        self.assertIs(result, expected)
        self.assertEqual(len(seen), 1)

        class ExpectedFailure(RuntimeError):
            pass

        failure = ExpectedFailure("保留原始失败")

        def fail(*args, **kwargs):
            raise failure

        with self.assertRaises(ExpectedFailure) as raised:
            analyze_pipeline_llm_chunks(
                [],
                "测试主播",
                "checkpoint.json",
                "legacy.json",
                seed_artifact_from_legacy=lambda *_args: None,
                analyze_topic_chunks=fail,
            )
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
