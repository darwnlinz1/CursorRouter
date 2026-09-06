
class TradingEngine:
    def __init__(self, initial_cash=50000.0):
        self.cash = initial_cash
        self.portfolio = {}

    def execute_order(self, symbol, side, shares, price):
        total = shares * price
        if side.upper() == 'BUY':
            if self.cash < total:
                raise ValueError("Insufficient buying power!")
            self.cash -= total
            pos = self.portfolio.get(symbol, {'shares': 0.0, 'avg_cost': 0.0})
            new_shares = pos['shares'] + shares
            new_cost = ((pos['shares'] * pos['avg_cost']) + total) / new_shares
            self.portfolio[symbol] = {'shares': new_shares, 'avg_cost': new_cost}
            return {'status': 'FILLED', 'side': 'BUY', 'symbol': symbol, 'shares': shares, 'price': price}
        elif side.upper() == 'SELL':
            pos = self.portfolio.get(symbol, {'shares': 0.0, 'avg_cost': 0.0})
            if pos['shares'] < shares:
                raise ValueError("Insufficient shares to sell!")
            self.cash += total
            rem = pos['shares'] - shares
            if rem <= 0.0001:
                del self.portfolio[symbol]
            else:
                self.portfolio[symbol]['shares'] = rem
            return {'status': 'FILLED', 'side': 'SELL', 'symbol': symbol, 'shares': shares, 'price': price}

    def calculate_pnl(self, current_prices):
        equity = self.cash
        unrealized = 0.0
        for sym, pos in self.portfolio.items():
            market_price = current_prices.get(sym, pos['avg_cost'])
            val = pos['shares'] * market_price
            cost = pos['shares'] * pos['avg_cost']
            unrealized += (val - cost)
            equity += val
        return {
            'cash': round(self.cash, 2),
            'equity': round(equity, 2),
            'unrealized_pnl': round(unrealized, 2),
            'positions_count': len(self.portfolio)
        }
