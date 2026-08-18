import unittest

from autoslice.analysis import response_parsing


class ResponseParsingTests(unittest.TestCase):
    def test_explicit_no_slice_hint_overrides_every_positive_marker(self):
        for hint in response_parsing.NO_SLICE_HINTS:
            title = f"✂️ 高能片段（{hint}）"
            with self.subTest(hint=hint):
                self.assertFalse(response_parsing.is_slice_marked(title))
                self.assertFalse(response_parsing.json_can_slice(True, title))
                self.assertFalse(response_parsing.json_can_slice("yes", title))

    def test_markdown_slice_marker_requires_scissors(self):
        self.assertTrue(response_parsing.is_slice_marked("✂ 高能片段"))
        self.assertTrue(response_parsing.is_slice_marked("✂️ 高能片段"))
        self.assertFalse(response_parsing.is_slice_marked("普通完整话题"))

    def test_json_boolean_numeric_and_string_values_keep_compatibility(self):
        truthy_values = (True, 1, -1, 0.5, "true", "TRUE", " yes ", "y", "1", "可切", "切", "是")
        falsey_values = (False, 0, 0.0, "false", "no", "否", "", None)

        for value in truthy_values:
            with self.subTest(value=value):
                self.assertTrue(response_parsing.json_can_slice(value, "普通标题"))
        for value in falsey_values:
            with self.subTest(value=value):
                self.assertFalse(response_parsing.json_can_slice(value, "普通标题"))

    def test_unknown_json_value_falls_back_to_title_marker(self):
        unknown_value = object()

        self.assertTrue(
            response_parsing.json_can_slice(unknown_value, "✂ 兼容标题"),
        )
        self.assertFalse(
            response_parsing.json_can_slice(unknown_value, "普通标题"),
        )


if __name__ == "__main__":
    unittest.main()
