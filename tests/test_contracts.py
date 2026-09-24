import unittest

from src.pottery import CraftStep, validate_batch_code


class PotteryContractTests(unittest.TestCase):
    def test_batch_code(self):
        self.assertEqual(validate_batch_code("KILN-2026-017"), "KILN-2026-017")

    def test_batch_code_rejects_short_year(self):
        with self.assertRaises(ValueError):
            validate_batch_code("KILN-26-1")

    def test_firing_step(self):
        self.assertEqual(CraftStep.FIRING.value, "firing")


if __name__ == "__main__":
    unittest.main()
