# Portfolio Analytics & Risk Decision Engine

A quantitative portfolio management dashboard built with Python (Flask) and SQLite. The application calculates core risk and performance metrics—such as Parametric Value at Risk (VaR), portfolio volatility, benchmark alpha, and Monte Carlo projections—to provide clear risk diagnostics without automated buy/sell recommendations.

---

## Key Capabilities

- **Risk Diagnostics**: Calculates 95% 1-day Value at Risk (VaR), annualized portfolio volatility, and asset concentration thresholds.
- **Scenario Modeling**: 250-iteration Monte Carlo simulations across 1-year horizons using Geometric Brownian Motion (GBM).
- **Technical Indicators**: Evaluates asset momentum using 14-day RSI and trend direction relative to the 200-day Simple Moving Average (SMA).
- **Benchmark Performance**: Measures active portfolio return against regional benchmark indices (Nifty 50 for INR, S&P 500 for USD) to calculate Alpha.
- **Portfolio Accounting**: Tracks open holdings and computes realized profit and loss on closed positions using weighted average cost basis.
- **Currency Support**: Native INR support with real-time conversion to USD via live exchange rates.
- **Account Isolation & Persistence**: Multi-user authentication using salted SHA-256 password hashing and persistent 30-day session cookies.
- **Data Management**: Dynamic position exclusion ("What-If" modeling) and complete CSV import/export for trade journals.

---

## Technical Stack

- **Backend**: Python 3, Flask, Gunicorn
- **Database**: SQLite3
- **Market Data**: Yahoo Finance API
- **Frontend**: HTML5, CSS3, Chart.js

---

## Getting Started

### 1. Clone the Repository
```bash
git clone [https://github.com/athilaj-gif/algorithmic-portfolio.git](https://github.com/athilaj-gif/algorithmic-portfolio.git)
cd algorithmic-portfolio
