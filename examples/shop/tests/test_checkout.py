import unittest

from shop.checkout import checkout


class CheckoutTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(checkout(5, 3), 15)
