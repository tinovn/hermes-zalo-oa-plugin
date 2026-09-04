"""Sổ cửa sổ tư vấn — thứ đứng giữa bot và hoá đơn Zalo.

Luật: miễn phí trong 48h kể từ tương tác cuối, còn gửi được (mất phí) tới 7
ngày, quá 7 ngày là API từ chối. Mặc định plugin CHẶN mọi tin ngoài 48h.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oa_window import ConsultationWindow  # noqa: E402

HOUR = 3600.0
DAY = 86400.0
NOW = 1_800_000_000.0


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "window.json"

    def w(self, allow_paid=False):
        return ConsultationWindow(self.path, allow_paid_window=allow_paid)

    def test_unknown_user_is_blocked(self):
        # Chưa từng thấy tương tác = tin chủ động gửi người lạ — ca đốt tiền.
        d = self.w().evaluate("u1", now=NOW)
        self.assertFalse(d.allowed)
        self.assertIsNone(d.seconds_since)

    def test_fresh_inbound_is_free(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        d = w.evaluate("u1", now=NOW + 60)
        self.assertTrue(d.allowed)
        self.assertTrue(d.within_free)

    def test_47h_still_free(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        self.assertTrue(w.evaluate("u1", now=NOW + 47 * HOUR).within_free)

    def test_49h_blocked_by_default(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        d = w.evaluate("u1", now=NOW + 49 * HOUR)
        self.assertFalse(d.allowed)
        self.assertTrue(d.within_api)      # API vẫn gửi được…
        self.assertFalse(d.within_free)    # …nhưng mất phí nên ta chặn
        self.assertIn("ZALO_OA_ALLOW_PAID_WINDOW", d.reason)

    def test_49h_allowed_when_opted_in(self):
        w = self.w(allow_paid=True)
        w.mark_inbound("u1", now=NOW)
        d = w.evaluate("u1", now=NOW + 49 * HOUR)
        self.assertTrue(d.allowed)
        self.assertFalse(d.within_free)
        self.assertIn("PHÍ", d.reason.upper())

    def test_beyond_7_days_blocked_even_when_paying(self):
        w = self.w(allow_paid=True)
        w.mark_inbound("u1", now=NOW)
        d = w.evaluate("u1", now=NOW + 8 * DAY)
        self.assertFalse(d.allowed)
        self.assertFalse(d.within_api)

    def test_new_inbound_reopens_window_and_resets_counter(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        w.note_outbound("u1")
        w.note_outbound("u1")
        self.assertEqual(w.sent_count("u1"), 2)
        w.mark_inbound("u1", now=NOW + 5 * DAY)
        self.assertEqual(w.sent_count("u1"), 0)
        self.assertTrue(w.evaluate("u1", now=NOW + 5 * DAY + 60).within_free)

    def test_users_are_independent(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        self.assertTrue(w.evaluate("u1", now=NOW).allowed)
        self.assertFalse(w.evaluate("u2", now=NOW).allowed)

    def test_state_survives_restart(self):
        w = self.w()
        w.mark_inbound("u1", now=NOW)
        w.note_outbound("u1")
        again = self.w()
        self.assertEqual(again.sent_count("u1"), 1)
        self.assertTrue(again.evaluate("u1", now=NOW + HOUR).allowed)

    def test_corrupt_file_starts_empty_instead_of_crashing(self):
        self.path.write_text("{not json", encoding="utf-8")
        w = self.w()
        self.assertFalse(w.evaluate("u1", now=NOW).allowed)
        w.mark_inbound("u1", now=NOW)  # vẫn ghi được đè lên file hỏng
        self.assertTrue(w.evaluate("u1", now=NOW).allowed)

    def test_prunes_only_stale_entries(self):
        w = self.w()
        for i in range(1100):
            w.mark_inbound(f"old{i}", now=NOW - 40 * DAY)
        w.mark_inbound("fresh", now=NOW)
        self.assertTrue(w.evaluate("fresh", now=NOW).allowed)
        self.assertLess(len(w._state), 1100)


if __name__ == "__main__":
    unittest.main()
