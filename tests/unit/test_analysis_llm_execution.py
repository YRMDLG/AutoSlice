import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from autoslice.analysis import llm_execution


class LLMExecutionTests(unittest.TestCase):
    def test_concurrency_defaults_and_clamps_environment_value(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOSLICE_LLM_CONCURRENCY", None)
            self.assertEqual(
                llm_execution.configured_llm_concurrency(),
                llm_execution.LLM_DEFAULT_CONCURRENCY,
            )

        expected_values = {
            "invalid": llm_execution.LLM_DEFAULT_CONCURRENCY,
            "": llm_execution.LLM_DEFAULT_CONCURRENCY,
            "-8": 1,
            "0": 1,
            "1": 1,
            "2": 2,
            "4": 4,
            "99": llm_execution.LLM_MAX_CONCURRENCY,
        }
        for raw_value, expected in expected_values.items():
            with self.subTest(raw_value=raw_value):
                with patch.dict(
                    os.environ,
                    {"AUTOSLICE_LLM_CONCURRENCY": raw_value},
                ):
                    self.assertEqual(
                        llm_execution.configured_llm_concurrency(),
                        expected,
                    )

    def test_empty_progress_callback_stays_disabled(self):
        self.assertIsNone(llm_execution.serialized_progress_callback(None))

    def test_progress_callback_calls_are_serialized_across_workers(self):
        state_lock = threading.Lock()
        start_barrier = threading.Barrier(4)
        calls = []
        active = 0
        max_active = 0

        def callback(message, step, total):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with state_lock:
                calls.append((message, step, total))
                active -= 1

        report = llm_execution.serialized_progress_callback(callback)
        self.assertIsNotNone(report)

        def worker(index):
            start_barrier.wait()
            report(f"消息 {index}", index, 4)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(worker, range(4)))

        self.assertEqual(max_active, 1)
        self.assertEqual(
            sorted(calls),
            [(f"消息 {index}", index, 4) for index in range(4)],
        )


if __name__ == "__main__":
    unittest.main()
