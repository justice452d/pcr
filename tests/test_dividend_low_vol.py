import unittest

from valuation_models.common import calculate_percentile_rank
from valuation_models.dividend_low_vol import (
    DEFAULT_WEIGHTS,
    calculate_series,
    fixed_spread_temperature,
    temperature_state,
)


def row(day, dividend=4.0, pb=1.0, bond=2.0, price=1000):
    return {"date": day, "dividend_yield": dividend, "pb": pb,
            "cn10y": bond, "index_price": price, "source": "test"}


class DividendLowVolModelTests(unittest.TestCase):
    def test_percentile_uses_less_than_or_equal(self):
        self.assertEqual(calculate_percentile_rank([1, 2, 2, 3], 2), 75)

    def test_dividend_percentile_direction_is_reversed(self):
        result = calculate_series([row("2026-01-01", 5), row("2026-01-02", 2)])
        self.assertLess(result[0]["dividend_temperature"], result[-1]["dividend_temperature"])

    def test_pb_direction_is_not_reversed(self):
        result = calculate_series([row("2026-01-01", pb=1.1), row("2026-01-02", pb=.7)])
        self.assertGreater(result[0]["pb_temperature"], result[-1]["pb_temperature"])

    def test_spread_direction(self):
        self.assertLess(fixed_spread_temperature(4.1), fixed_spread_temperature(1.2))

    def test_default_weights(self):
        self.assertEqual(DEFAULT_WEIGHTS, {"dividend": 45.0, "spread": 35.0, "pb": 20.0})

    def test_temperature_is_clamped(self):
        result = calculate_series([row("2026-01-01")])[-1]["valuation_temperature"]
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 100)

    def test_missing_required_data_has_no_temperature(self):
        result = calculate_series([row("2026-01-01", pb=None)])[-1]
        self.assertIsNone(result["valuation_temperature"])
        self.assertIn("PB", result["missing"])

    def test_state_boundaries_do_not_overlap(self):
        expected = {0: "极度便宜", 19.9: "极度便宜", 20: "便宜", 40: "正常",
                    60: "偏贵", 75: "贵", 90: "极贵", 100: "极贵"}
        for value, state in expected.items():
            with self.subTest(value=value):
                self.assertEqual(temperature_state(value), state)

    def test_new_history_updates_percentile(self):
        before = calculate_percentile_rank([1, 2], 2)
        after = calculate_percentile_rank([1, 2, 3], 2)
        self.assertEqual(before, 100)
        self.assertAlmostEqual(after, 66.6666666667)


if __name__ == "__main__":
    unittest.main()

