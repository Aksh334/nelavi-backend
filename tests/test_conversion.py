import unittest

from calibration.conversion import mg_per_kg_to_kg_per_ha


class TestMgPerKgToKgPerHa(unittest.TestCase):
    def test_zero_input_gives_zero(self):
        self.assertEqual(mg_per_kg_to_kg_per_ha(0), 0)

    def test_100_mg_per_kg_is_224_kg_per_ha(self):
        self.assertAlmostEqual(mg_per_kg_to_kg_per_ha(100), 224.0, places=9)

    def test_one_mg_per_kg_is_the_factor(self):
        self.assertAlmostEqual(mg_per_kg_to_kg_per_ha(1), 2.24, places=9)

    def test_fractional_input(self):
        self.assertAlmostEqual(mg_per_kg_to_kg_per_ha(12.5), 28.0, places=9)


if __name__ == "__main__":
    unittest.main()
