import unittest

from calibration.npk_correction import correct_npk


class TestCorrectNpk(unittest.TestCase):
    def test_simple_positive_case(self):
        # 1*10 + 1*5 + 0 = 15
        self.assertEqual(correct_npk(probe=10, moisture=5, a=1, b=0, c=1), 15)

    def test_non_zero_intercept(self):
        # 1*10 + 1*5 + 3 = 18
        self.assertEqual(correct_npk(probe=10, moisture=5, a=1, b=3, c=1), 18)

    def test_coefficients_not_all_one(self):
        # 2*10 + 0.5*4 + 1 = 23
        self.assertEqual(correct_npk(probe=10, moisture=4, a=2, b=1, c=0.5), 23)

    def test_zero_moisture_coefficient_ignores_moisture(self):
        # 3*4 + 0*99 + 2 = 14
        self.assertEqual(correct_npk(probe=4, moisture=99, a=3, b=2, c=0), 14)

    def test_zero_probe_coefficient_ignores_probe(self):
        # 0*100 + 2*3 + (-1) = 5
        self.assertEqual(correct_npk(probe=100, moisture=3, a=0, b=-1, c=2), 5)

    def test_result_is_not_clamped(self):
        # 1*4 + 0*0 + (-10) = -6
        self.assertEqual(correct_npk(probe=4, moisture=0, a=1, b=-10, c=0), -6)


if __name__ == "__main__":
    unittest.main()
