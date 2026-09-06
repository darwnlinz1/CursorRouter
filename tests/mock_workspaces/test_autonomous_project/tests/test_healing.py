
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analytics import compute_sharpe_ratio

class TestAnalytics(unittest.TestCase):
    def test_empty_and_flat_returns(self):
        self.assertEqual(compute_sharpe_ratio([]), 0.0)
        self.assertEqual(compute_sharpe_ratio([0.05, 0.05, 0.05]), 0.0)

if __name__ == '__main__':
    unittest.main()
