import unittest

from service.research.content import assess_content


ARTICLE = "This research explains browser automation and the limitations of detection. " * 40
YANDEX_GATE = (
    "[](https://www.yandex.com)\n"
    "# Please confirm that you are not a robot\n"
    "We're sorry, but it looks like requests sent from your device are automated. "
    "Please confirm that you are not a robot.\n"
    "I'm not a robot Press to continue\n"
    "[Yandex SmartCaptcha](https://yandex.com/legal/smartcaptcha_notice/)\n"
)


class ContentGateTests(unittest.TestCase):
    def test_yandex_gate_with_leading_link(self):
        self.assertEqual(
            assess_content(YANDEX_GATE, "Are you not a robot?"),
            {"status": "blocked", "reasons": ["challenge_or_access_gate"]},
        )

    def test_primary_heading_after_link_without_gate_title(self):
        self.assertEqual(assess_content(YANDEX_GATE, "Yandex")["status"], "blocked")

    def test_russian_gate_headings(self):
        for heading in ("Подтвердите, что вы не робот", "Вы не робот?"):
            with self.subTest(heading=heading):
                self.assertEqual(
                    assess_content("[](https://ya.ru)\n# " + heading + "\nПродолжить")["status"],
                    "blocked",
                )

    def test_substantial_gate_body(self):
        self.assertEqual(assess_content(YANDEX_GATE + ARTICLE)["status"], "blocked")

    def test_smartcaptcha_gate_title(self):
        self.assertEqual(
            assess_content("I'm not a robot\nPress to continue", "Yandex SmartCaptcha")["status"],
            "blocked",
        )

    def test_articles_about_captcha_remain_readable(self):
        for title in (
            "CAPTCHA research",
            "SmartCaptcha",
            "Yandex SmartCaptcha integration guide",
            "Why sites ask: Are you not a robot?",
            "How to complete the CAPTCHA",
            "Security verification methods",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    assess_content("# " + title + "\n" + ARTICLE, title)["status"], "read"
                )

    def test_article_secondary_gate_heading_is_not_a_gate(self):
        markdown = "# CAPTCHA research\n" + ARTICLE + "\n## Verify you are human\n"
        self.assertEqual(assess_content(markdown, "CAPTCHA research")["status"], "read")

    def test_http_empty_and_partial_results_preserved(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.assertEqual(
                    assess_content(ARTICLE, status_code=status),
                    {"status": "blocked", "reasons": [f"http_gate_{status}"]},
                )
        self.assertEqual(assess_content(ARTICLE, status_code=404)["status"], "failed")
        self.assertEqual(assess_content("")["reasons"], ["empty_content"])
        self.assertEqual(assess_content("Short extract")["status"], "partial")
        self.assertEqual(assess_content(ARTICLE, status_code=None)["status"], "read")
