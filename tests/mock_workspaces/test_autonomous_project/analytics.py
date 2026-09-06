
def compute_sharpe_ratio(returns, risk_free_rate=0.02):
    if not returns or len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    if variance <= 1e-9:
        return 0.0
    std_dev = variance ** 0.5
    return round((mean_ret - risk_free_rate) / std_dev, 4)
