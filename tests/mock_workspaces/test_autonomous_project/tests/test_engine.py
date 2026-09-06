
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import TradingEngine

class TestEngine(unittest.TestCase):
    def test_trade_lifecycle(self):
        e = TradingEngine(initial_cash=10000.0)
        e.execute_order('NVDA', 'BUY', 10, 120.0)
        self.assertEqual(e.cash, 8800.0)
        self.assertEqual(e.portfolio['NVDA']['shares'], 10)
        
        pnl = e.calculate_pnl({'NVDA': 150.0})
        self.assertEqual(pnl['equity'], 10300.0)
        self.assertEqual(pnl['unrealized_pnl'], 300.0)

        e.execute_order('NVDA', 'SELL', 5, 150.0)
        self.assertEqual(e.cash, 9550.0)
        self.assertEqual(e.portfolio['NVDA']['shares'], 5)

if __name__ == '__main__':
    unittest.main()
