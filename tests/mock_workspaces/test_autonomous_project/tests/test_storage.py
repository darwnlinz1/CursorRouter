
import unittest
import sqlite3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage import init_db, get_balance, record_transaction

class TestStorage(unittest.TestCase):
    def setUp(self):
        self.db = 'test_fintrack.db'
        if os.path.exists(self.db):
            os.remove(self.db)
        init_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_deposit_and_balance(self):
        record_transaction(self.db, 'USD', 10000.0, 'DEPOSIT')
        bal = get_balance(self.db, 'USD')
        self.assertEqual(bal, 10000.0)

    def test_trade_deduction(self):
        record_transaction(self.db, 'USD', 5000.0, 'DEPOSIT')
        record_transaction(self.db, 'USD', -2000.0, 'BUY_AAPL')
        bal = get_balance(self.db, 'USD')
        self.assertEqual(bal, 3000.0)

if __name__ == '__main__':
    unittest.main()
