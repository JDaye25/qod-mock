import unittest

from backend.main import choose_qos_profile, Intent


class UnitTests(unittest.TestCase):
    def test_choose_qos_profile_thresholds(self):
        # <= 50 => low latency
        i1 = Intent(text="x", target_p95_latency_ms=50, target_jitter_ms=0, duration_s=10)
        self.assertEqual(choose_qos_profile(i1), "QOS_LOW_LATENCY")

        # 51..150 => balanced
        i2 = Intent(text="x", target_p95_latency_ms=51, target_jitter_ms=0, duration_s=10)
        self.assertEqual(choose_qos_profile(i2), "QOS_BALANCED")

        i3 = Intent(text="x", target_p95_latency_ms=150, target_jitter_ms=0, duration_s=10)
        self.assertEqual(choose_qos_profile(i3), "QOS_BALANCED")

        # > 150 => best effort
        i4 = Intent(text="x", target_p95_latency_ms=151, target_jitter_ms=0, duration_s=10)
        self.assertEqual(choose_qos_profile(i4), "QOS_BEST_EFFORT")
        