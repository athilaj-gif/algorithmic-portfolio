import csv
import hashlib
import io
import json
import math
import os
import random
import sqlite3
import urllib.request
from datetime import date, timedelta
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template_string,
    request,
    session,
)

# Optional dotenv support for local development
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# --- GitHub-Safe Session & Secret Key Configuration ---
app.secret_key = os.environ.get(
    "SECRET_KEY", "dev-fallback-portfolio-key-do-not-use-in-production"
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

DB_FILE = "portfolio.db"


# --- Database Schema with Self-Healing Migrations ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Users table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL
        )
    """
    )

    # Transactions table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            ticker TEXT NOT NULL,
            transaction_type TEXT DEFAULT 'BUY',
            shares REAL NOT NULL,
            price REAL NOT NULL DEFAULT 0.0,
            trade_date TEXT NOT NULL DEFAULT '',
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """
    )

    # Inspect columns dynamically to support older databases cleanly
    c.execute("PRAGMA table_info(transactions)")
    cols = [r[1] for r in c.fetchall()]

    if "buy_price" in cols and "price" not in cols:
        try:
            c.execute(
                "ALTER TABLE transactions RENAME COLUMN buy_price TO price"
            )
        except sqlite3.OperationalError:
            c.execute(
                "ALTER TABLE transactions ADD COLUMN price REAL DEFAULT 0.0"
            )
            c.execute("UPDATE transactions SET price = buy_price")

    if "buy_date" in cols and "trade_date" not in cols:
        try:
            c.execute(
                "ALTER TABLE transactions RENAME COLUMN buy_date TO trade_date"
            )
        except sqlite3.OperationalError:
            c.execute(
                "ALTER TABLE transactions ADD COLUMN trade_date TEXT DEFAULT ''"
            )
            c.execute("UPDATE transactions SET trade_date = buy_date")

    if "transaction_type" not in cols:
        try:
            c.execute(
                "ALTER TABLE transactions ADD COLUMN transaction_type TEXT DEFAULT 'BUY'"
            )
        except sqlite3.OperationalError:
            pass

    if "user_id" not in cols:
        try:
            c.execute(
                "ALTER TABLE transactions ADD COLUMN user_id INTEGER DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass

    if "is_active" not in cols:
        try:
            c.execute(
                "ALTER TABLE transactions ADD COLUMN is_active INTEGER DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


init_db()


# --- Security: Pure Python Password Hashing ---
def hash_password(password, salt=None):
    if not salt:
        salt = os.urandom(16).hex()
    hashed = hashlib.sha256((password + salt).encode("utf-8")).hexdigest()
    return hashed, salt


# --- Market Data Fetcher via Yahoo Finance Chart API ---
def fetch_stock_history(ticker):
    clean_ticker = ticker.strip().upper()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_ticker}?interval=1d&range=1y"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode())
            result = data["chart"]["result"][0]
            meta = result["meta"]
            currency = meta.get("currency", "USD").upper()

            closes = result["indicators"]["quote"][0].get("close", [])
            valid_closes = [float(c) for c in closes if c is not None]
            current_price = (
                meta.get("regularMarketPrice")
                or meta.get("chartPreviousClose")
                or (valid_closes[-1] if valid_closes else 0.0)
            )

            return {
                "ticker": clean_ticker,
                "current_price": float(current_price),
                "currency": currency,
                "history": valid_closes,
            }
    except Exception:
        default_curr = (
            "INR"
            if clean_ticker.endswith(".NS") or clean_ticker.endswith(".BO")
            else "USD"
        )
        return {
            "ticker": clean_ticker,
            "current_price": None,
            "currency": default_curr,
            "history": [],
        }


def get_usd_to_inr_rate():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X?interval=1d&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            meta = data["chart"]["result"][0]["meta"]
            return float(
                meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
            )
    except Exception:
        return 86.50


def fetch_benchmark_return(display_curr):
    benchmark_ticker = "^NSEI" if display_curr == "INR" else "^GSPC"
    name = "Nifty 50" if display_curr == "INR" else "S&P 500"
    history_data = fetch_stock_history(benchmark_ticker)["history"]
    if len(history_data) >= 2:
        start_p = history_data[0]
        end_p = history_data[-1]
        pct = ((end_p - start_p) / start_p) * 100
        return {"name": name, "return_pct": round(pct, 2)}
    return {"name": name, "return_pct": 12.0}


# --- Financial & Risk Analytics ---
def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-period - 1 + i] - prices[-period - 1 + i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_sma(prices, period=200):
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0
    return sum(prices[-period:]) / period


def compute_annualized_volatility(prices):
    if len(prices) < 5:
        return 0.20
    returns = [
        (prices[i] - prices[i - 1]) / prices[i - 1]
        for i in range(1, len(prices))
        if prices[i - 1] > 0
    ]
    if not returns:
        return 0.20
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(252)


def run_monte_carlo(current_total, weighted_vol, weighted_mu=0.10, days=252, runs=250):
    step_indices = list(range(0, days + 1, 21))
    step_labels = [f"+{int(i/21)}M" if i > 0 else "Base" for i in step_indices]

    if current_total <= 0:
        zeros = [0.0] * len(step_indices)
        return {
            "labels": step_labels,
            "median": zeros,
            "p10": zeros,
            "p90": zeros,
        }

    dt = 1 / 252.0
    all_trajectories = []

    for _ in range(runs):
        val = current_total
        path = [val]
        for _ in range(days):
            u1 = max(1e-9, random.random())
            u2 = random.random()
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
            growth = (weighted_mu - 0.5 * (weighted_vol**2)) * dt + (
                weighted_vol * math.sqrt(dt) * z
            )
            val *= math.exp(growth)
            path.append(val)
        all_trajectories.append(path)

    median_line, p10_line, p90_line = [], [], []
    for idx in step_indices:
        vals_at_step = sorted([traj[idx] for traj in all_trajectories])
        median_line.append(round(vals_at_step[int(0.50 * runs)], 2))
        p10_line.append(round(vals_at_step[int(0.10 * runs)], 2))
        p90_line.append(round(vals_at_step[int(0.90 * runs)], 2))

    return {
        "labels": step_labels,
        "median": median_line,
        "p10": p10_line,
        "p90": p90_line,
    }


# --- UI Templates ---
AUTH_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ title }} - Portfolio Analytics Engine</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: #0b1120; color: #f1f5f9; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }
        .auth-card { background: #131d31; border: 1px solid #1e293b; border-radius: 10px; width: 100%; max-width: 400px; padding: 32px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h2 { margin-top: 0; font-size: 20px; font-weight: 600; text-align: center; }
        .sub { color: #94a3b8; font-size: 13px; text-align: center; margin-bottom: 24px; }
        label { font-size: 12px; color: #94a3b8; display: block; margin-top: 14px; margin-bottom: 6px; }
        input { width: 100%; padding: 10px 12px; border-radius: 6px; border: 1px solid #334155; background: #0b1120; color: white; font-size: 14px; }
        button { width: 100%; padding: 11px; border-radius: 6px; border: none; background: #2563eb; color: white; font-weight: 600; font-size: 14px; margin-top: 20px; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        .footer-link { text-align: center; margin-top: 20px; font-size: 13px; color: #94a3b8; }
        .footer-link a { color: #38bdf8; text-decoration: none; font-weight: 500; }
        .flash { background: #7f1d1d; color: #fecaca; padding: 10px; border-radius: 6px; font-size: 13px; margin-bottom: 16px; text-align: center; }
    </style>
</head>
<body>
    <div class="auth-card">
        <h2>{{ title }}</h2>
        <div class="sub">Algorithmic Portfolio Risk Engine</div>
        
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <form method="POST">
            <label>Username</label>
            <input type="text" name="username" required autocomplete="username">
            <label>Password</label>
            <input type="password" name="password" required autocomplete="current-password">
            <button type="submit">{{ button_text }}</button>
        </form>

        <div class="footer-link">
            {% if is_login %}
                Don't have an account? <a href="/signup">Sign up</a>
            {% else %}
                Already registered? <a href="/login">Log in</a>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Portfolio Analytics - {{ user.username }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: #0b1120; color: #f1f5f9; margin: 0; padding: 24px; }
        .container { max-width: 1240px; margin: auto; }
        
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
        h1 { margin: 0; font-size: 22px; font-weight: 600; }
        .sub { color: #94a3b8; font-size: 13px; margin-top: 4px; }
        .top-actions { display: flex; align-items: center; gap: 12px; }

        .user-tag { background: #1e293b; color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: 600; border: 1px solid #334155; }
        .logout-link { color: #f87171; text-decoration: none; font-size: 13px; margin-left: 8px; font-weight: normal; }
        .logout-link:hover { text-decoration: underline; }

        .help-guide-btn {
            background: #1e293b; color: #93c5fd; border: 1px solid #3b82f6;
            padding: 7px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
            cursor: pointer; display: flex; align-items: center; gap: 6px;
        }
        .help-guide-btn:hover { background: #1e3a8a; color: #ffffff; }

        .currency-switch { display: flex; gap: 4px; background: #1e293b; padding: 4px; border-radius: 6px; border: 1px solid #334155; }
        .currency-btn { padding: 6px 14px; border-radius: 4px; text-decoration: none; font-size: 13px; font-weight: 500; color: #94a3b8; }
        .currency-btn.active { background: #2563eb; color: #ffffff; }

        .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
        .metric-card { background: #131d31; padding: 18px; border-radius: 8px; border: 1px solid #1e293b; position: relative; }
        .metric-label { font-size: 12px; color: #94a3b8; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center; justify-content: space-between; }
        .metric-value { font-size: 22px; font-weight: 600; margin-top: 8px; }
        .metric-desc { font-size: 12px; color: #64748b; margin-top: 4px; }

        .info-tip {
            display: inline-flex; align-items: center; justify-content: center;
            width: 16px; height: 16px; border-radius: 50%;
            background: #1e293b; color: #94a3b8; font-size: 11px;
            cursor: help; position: relative; border: 1px solid #475569;
        }
        .info-tip:hover { background: #2563eb; color: white; border-color: #3b82f6; }
        .info-tip:hover::after {
            content: attr(data-tooltip);
            position: absolute; bottom: 125%; left: 50%; transform: translateX(-50%);
            background: #020617; color: #e2e8f0; padding: 10px 12px; border-radius: 6px;
            font-size: 12px; font-weight: 400; line-height: 1.4;
            min-width: 230px; max-width: 290px; z-index: 1000;
            border: 1px solid #334155; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
            pointer-events: none; text-transform: none;
        }

        .summary-card { background: #152238; border-left: 3px solid #3b82f6; padding: 14px 18px; border-radius: 6px; margin-bottom: 24px; font-size: 13px; color: #cbd5e1; line-height: 1.6; }
        
        .main-grid { display: grid; grid-template-columns: 320px 1fr; gap: 24px; margin-bottom: 24px; }
        .card { background: #131d31; padding: 20px; border-radius: 8px; border: 1px solid #1e293b; }
        h3 { margin-top: 0; margin-bottom: 16px; font-size: 15px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between; align-items: center; }
        
        label { font-size: 12px; color: #94a3b8; display: block; margin-top: 10px; margin-bottom: 4px; }
        input, select, button { width: 100%; padding: 10px 12px; border-radius: 6px; border: 1px solid #334155; background: #0b1120; color: white; font-size: 13px; }
        button.primary-btn { background: #2563eb; color: white; font-weight: 500; border: none; cursor: pointer; margin-top: 16px; }
        button.primary-btn:hover { background: #1d4ed8; }

        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #1e293b; font-size: 13px; }
        th { color: #94a3b8; font-weight: 600; background: #0f172a; font-size: 12px; }
        
        .tag { padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; display: inline-block; }
        .tag-green { background: #064e3b; color: #a7f3d0; }
        .tag-yellow { background: #451a03; color: #fde68a; }
        .tag-red { background: #7f1d1d; color: #fecaca; }
        .tag-neutral { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; }
        
        .positive { color: #34d399; }
        .negative { color: #f87171; }

        .btn-delete {
            background: #450a0a; color: #f87171; border: 1px solid #7f1d1d;
            border-radius: 4px; padding: 4px 8px; font-size: 12px; cursor: pointer;
        }
        .btn-delete:hover { background: #7f1d1d; color: white; }

        .row-dimmed { opacity: 0.45; text-decoration: line-through; }
        .checkbox-custom { width: 16px; height: 16px; cursor: pointer; accent-color: #2563eb; margin: 0; }

        .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }
        .chart-container { height: 260px; position: relative; }
        .ledger-box { background: #131d31; padding: 20px; border-radius: 8px; border: 1px solid #1e293b; margin-top: 24px; }

        .csv-toolbar { display: flex; gap: 12px; align-items: center; justify-content: flex-end; margin-bottom: 12px; }
        .btn-secondary {
            background: #1e293b; color: #cbd5e1; border: 1px solid #475569;
            padding: 7px 12px; border-radius: 6px; font-size: 12px; font-weight: 500;
            cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
        }
        .btn-secondary:hover { background: #334155; color: white; }

        /* Guide Modal */
        .modal-overlay {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.75); z-index: 2000; align-items: center; justify-content: center;
        }
        .modal {
            background: #131d31; border: 1px solid #334155; border-radius: 10px;
            width: 90%; max-width: 680px; max-height: 85vh; overflow-y: auto; padding: 24px;
        }
        .modal-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 12px; margin-bottom: 16px; }
        .modal-header h2 { margin: 0; font-size: 18px; color: #f1f5f9; }
        .close-btn { background: none; border: none; color: #94a3b8; font-size: 22px; cursor: pointer; width: auto; padding: 0; margin: 0; }
        .close-btn:hover { color: white; }
        .guide-item { margin-bottom: 16px; border-bottom: 1px solid #1e293b; padding-bottom: 12px; }
        .guide-item:last-child { border-bottom: none; }
        .guide-term { font-size: 14px; font-weight: 600; color: #38bdf8; margin-bottom: 4px; }
        .guide-text { font-size: 13px; color: #cbd5e1; line-height: 1.5; margin: 0; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div>
                <h1>Portfolio Analytics & Decision Engine</h1>
                <div class="sub">Quantitative risk analytics, scenario projections & benchmark comparison</div>
            </div>
            <div class="top-actions">
                <div class="user-tag">
                    👤 {{ user.username }}
                    <a href="/logout" class="logout-link">(Sign out)</a>
                </div>
                <button class="help-guide-btn" onclick="openModal()">
                    <span style="font-size: 15px;">💡</span> Understanding Metrics
                </button>
                <div class="currency-switch">
                    <a href="/?curr=INR" class="currency-btn {% if display_curr == 'INR' %}active{% endif %}">₹ INR</a>
                    <a href="/?curr=USD" class="currency-btn {% if display_curr == 'USD' %}active{% endif %}">$ USD</a>
                </div>
            </div>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">
                    Active Portfolio Value
                    <span class="info-tip" data-tooltip="Total current market worth of active open holdings.">ⓘ</span>
                </div>
                <div class="metric-value">{{ sym }}{{ "{:,.2f}".format(total_current) }}</div>
                <div class="metric-desc">Cost Basis: {{ sym }}{{ "{:,.2f}".format(total_invested) }}</div>
            </div>

            <div class="metric-card">
                <div class="metric-label">
                    Benchmark Comparison
                    <span class="info-tip" data-tooltip="Compares your portfolio return against {{ benchmark.name }} over a 1-year timeline.">ⓘ</span>
                </div>
                <div class="metric-value {% if alpha >= 0 %}positive{% else %}negative{% endif %}">
                    {{ "{:+.1f}%".format(alpha) }}
                </div>
                <div class="metric-desc">vs. {{ benchmark.name }} ({{ "{:+.1f}%".format(benchmark.return_pct) }})</div>
            </div>

            <div class="metric-card">
                <div class="metric-label">
                    Daily Downside Threshold
                    <span class="info-tip" data-tooltip="Value at Risk (95% VaR): Statistically, on 19 out of 20 normal trading days, 1-day drop will not exceed this.">ⓘ</span>
                </div>
                <div class="metric-value negative">-{{ sym }}{{ "{:,.2f}".format(daily_var) }}</div>
                <div class="metric-desc">95% confidence 1-day boundary</div>
            </div>

            <div class="metric-card">
                <div class="metric-label">
                    Realized P&amp;L (Closed)
                    <span class="info-tip" data-tooltip="Actual profit or loss locked in from past SELL transactions.">ⓘ</span>
                </div>
                <div class="metric-value {% if total_realized >= 0 %}positive{% else %}negative{% endif %}">
                    {{ sym }}{{ "{:,.2f}".format(total_realized) }}
                </div>
                <div class="metric-desc">Unrealized: {{ sym }}{{ "{:,.2f}".format(total_pnl) }}</div>
            </div>
        </div>

        <div class="summary-card">
            <b>Quantitative Risk Assessment:</b><br>
            • <b>Market Comparison:</b> Your active portfolio performance is currently <b>{{ "{:+.1f}%".format(alpha) }}</b> relative to the <b>{{ benchmark.name }}</b> index.<br>
            • <b>Daily Risk Boundary:</b> On 95% of normal trading days, projected daily variation is modeled not to exceed <b>{{ sym }}{{ "{:,.2f}".format(daily_var) }}</b>.<br>
            • <b>Concentration & Balance:</b> Your largest active security represents <b>{{ "{:.1f}%".format(max_weight) }}</b> of total capital (<b>{{ conc_label }}</b>).
        </div>

        <div class="main-grid">
            <div class="card">
                <h3>Record Trade</h3>
                <form method="POST" action="/add?curr={{ display_curr }}">
                    <label>Order Type</label>
                    <select name="transaction_type">
                        <option value="BUY">BUY (Acquire Shares)</option>
                        <option value="SELL">SELL (Liquidate / Exit)</option>
                    </select>
                    
                    <label>Ticker Symbol</label>
                    <input type="text" name="ticker" placeholder="e.g. RELIANCE.NS, TCS.NS, AAPL" required>
                    
                    <label>Shares</label>
                    <input type="number" step="any" name="shares" placeholder="10" required>
                    
                    <label>Execution Price (in {{ sym }})</label>
                    <input type="number" step="any" name="price" placeholder="1500" required>
                    
                    <label>Execution Date</label>
                    <input type="date" name="trade_date" value="{{ today }}" required>
                    
                    <button type="submit" class="primary-btn">Save to Ledger</button>
                </form>
            </div>

            <div class="card">
                <h3>
                    <span>Active Holdings &amp; Technical Matrix</span>
                    <small style="color: #64748b; font-weight: normal; font-size: 12px;">Uncheck box to exclude from analytics</small>
                </h3>
                {% if holdings %}
                <table>
                    <thead>
                        <tr>
                            <th style="width: 35px; text-align: center;">Active</th>
                            <th>Security</th>
                            <th>Weight</th>
                            <th>200-Day Trend</th>
                            <th>Momentum (RSI)</th>
                            <th>Unrealized Return</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for h in holdings %}
                        <tr class="{% if not h.is_active %}row-dimmed{% endif %}">
                            <td style="text-align: center;">
                                <form method="POST" action="/toggle/{{ h.ticker }}?curr={{ display_curr }}" style="margin: 0;">
                                    <input type="checkbox" class="checkbox-custom" {% if h.is_active %}checked{% endif %} onchange="this.form.submit()">
                                </form>
                            </td>
                            <td>
                                <b>{{ h.ticker }}</b><br>
                                <small style="color: #64748b;">{{ h.shares }} sh @ {{ sym }}{{ "{:,.1f}".format(h.current_price) }}</small>
                            </td>
                            <td>
                                <b>{{ "{:.1f}%".format(h.weight) }}</b>
                                {% if h.weight > 40 and h.is_active %}
                                    <br><span class="tag tag-yellow">High Exposure</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if h.price_vs_sma >= 0 %}
                                    <span class="tag tag-green">Above 200 SMA</span>
                                {% else %}
                                    <span class="tag tag-red">Below 200 SMA</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if h.rsi_mood == "Overextended" %}
                                    <span class="tag tag-yellow">Overextended ({{ h.rsi }})</span>
                                {% elif h.rsi_mood == "Oversold" %}
                                    <span class="tag tag-green">Oversold ({{ h.rsi }})</span>
                                {% else %}
                                    <span class="tag tag-neutral">Neutral ({{ h.rsi }})</span>
                                {% endif %}
                            </td>
                            <td class="{% if h.pnl >= 0 %}positive{% else %}negative{% endif %}">
                                <b>{{ sym }}{{ "{:,.2f}".format(h.pnl) }}</b><br>
                                <small>({{ "{:+.1f}%".format(h.pnl_pct) }})</small>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <p style="color: #94a3b8; font-size: 13px;">No active open positions. Record a BUY transaction on the left to begin.</p>
                {% endif %}
            </div>
        </div>

        <div class="charts-row">
            <div class="card">
                <h3>Portfolio Allocation</h3>
                <div class="chart-container">
                    <canvas id="allocChart"></canvas>
                </div>
            </div>
            <div class="card">
                <h3>1-Year Scenario Projections (Monte Carlo)</h3>
                <div class="chart-container">
                    <canvas id="mcChart"></canvas>
                </div>
            </div>
        </div>

        <div class="ledger-box">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0;">{{ user.username }}'s Recorded Trades</h3>
                <div class="csv-toolbar">
                    <a href="/export-csv" class="btn-secondary">📥 Export CSV</a>
                    <form method="POST" action="/import-csv?curr={{ display_curr }}" enctype="multipart/form-data" style="display: inline-flex; align-items: center; gap: 6px; margin: 0;">
                        <input type="file" name="csv_file" accept=".csv" required style="width: auto; padding: 4px; font-size: 11px;">
                        <button type="submit" class="btn-secondary">📤 Import CSV</button>
                    </form>
                </div>
            </div>
            
            {% if raw_transactions %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Type</th>
                        <th>Security</th>
                        <th>Shares</th>
                        <th>Price</th>
                        <th>Date</th>
                        <th style="text-align: right;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {% for t in raw_transactions %}
                    <tr>
                        <td style="color: #64748b;">#{{ t.id }}</td>
                        <td>
                            <span class="tag {% if t.type == 'BUY' %}tag-green{% else %}tag-red{% endif %}">
                                {{ t.type }}
                            </span>
                        </td>
                        <td><b>{{ t.ticker }}</b></td>
                        <td>{{ t.shares }}</td>
                        <td>{{ sym }}{{ "{:,.2f}".format(t.price) }}</td>
                        <td style="color: #94a3b8;">{{ t.trade_date }}</td>
                        <td style="text-align: right;">
                            <form method="POST" action="/delete/{{ t.id }}?curr={{ display_curr }}" style="margin: 0; display: inline;" onsubmit="return confirm('Delete this trade record?');">
                                <button type="submit" class="btn-delete">✕ Delete</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p style="color: #94a3b8; font-size: 13px;">Your trade ledger is empty.</p>
            {% endif %}
        </div>
    </div>

    <!-- Educational Guide Modal -->
    <div id="guideModal" class="modal-overlay" onclick="closeModalOnBg(event)">
        <div class="modal">
            <div class="modal-header">
                <h2>📖 Understanding Your Dashboard & Analytics</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div class="guide-item">
                <div class="guide-term">1. Benchmark Comparison &amp; Alpha</div>
                <p class="guide-text">Compares your portfolio return against your regional market benchmark (Nifty 50 for INR, S&amp;P 500 for USD). A positive Alpha means your active decisions beat the broader market index.</p>
            </div>
            <div class="guide-item">
                <div class="guide-term">2. Realized vs. Unrealized P&amp;L</div>
                <p class="guide-text"><b>Unrealized:</b> Gains or losses on currently open shares if liquidated right now.<br><b>Realized:</b> Concrete profits or losses locked into cash from past SELL transactions.</p>
            </div>
            <div class="guide-item">
                <div class="guide-term">3. Daily Downside Threshold (95% VaR)</div>
                <p class="guide-text">Models maximum expected 1-day decline under normal conditions with 95% statistical confidence.</p>
            </div>
            <div class="guide-item">
                <div class="guide-term">4. What-If Simulation</div>
                <p class="guide-text">Unchecking a security temporarily recalculates portfolio risk, volatility, and projections without removing the transaction from your permanent database history.</p>
            </div>
        </div>
    </div>

    <script>
        function openModal() { document.getElementById('guideModal').style.display = 'flex'; }
        function closeModal() { document.getElementById('guideModal').style.display = 'none'; }
        function closeModalOnBg(e) { if (e.target.id === 'guideModal') closeModal(); }

        const allocCtx = document.getElementById('allocChart');
        if (allocCtx) {
            new Chart(allocCtx, {
                type: 'doughnut',
                data: {
                    labels: {{ chart_labels | safe }},
                    datasets: [{
                        data: {{ chart_values | safe }},
                        backgroundColor: ['#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#06b6d4', '#64748b']
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'right', labels: { color: '#94a3b8' } } }
                }
            });
        }

        const mcCtx = document.getElementById('mcChart');
        if (mcCtx) {
            new Chart(mcCtx, {
                type: 'line',
                data: {
                    labels: {{ mc_labels | safe }},
                    datasets: [
                        {
                            label: 'Optimistic Scenario (90th Percentile)',
                            data: {{ mc_p90 | safe }},
                            borderColor: '#10b981',
                            borderDash: [4, 4],
                            tension: 0.2,
                            fill: false
                        },
                        {
                            label: 'Base Projection (Median)',
                            data: {{ mc_median | safe }},
                            borderColor: '#3b82f6',
                            borderWidth: 2,
                            tension: 0.2,
                            fill: false
                        },
                        {
                            label: 'Conservative Stress Test (10th Percentile)',
                            data: {{ mc_p10 | safe }},
                            borderColor: '#ef4444',
                            borderDash: [4, 4],
                            tension: 0.2,
                            fill: false
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8' } },
                        y: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8' } }
                    },
                    plugins: {
                        legend: { position: 'bottom', labels: { color: '#94a3b8', boxWidth: 12 } }
                    }
                }
            });
        }
    </script>
</body>
</html>
"""


# --- Authentication Routes ---
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"].strip()

        if len(username) < 3 or len(password) < 4:
            flash("Username (min 3 chars) and password (min 4 chars) required.")
            return render_template_string(
                AUTH_TEMPLATE,
                title="Create Account",
                button_text="Sign Up",
                is_login=False,
            )

        pwd_hash, salt = hash_password(password)
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute(
                "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                (username, pwd_hash, salt),
            )
            conn.commit()
            user_id = c.lastrowid
            conn.close()

            session.permanent = True
            session["user_id"] = user_id
            session["username"] = username
            return redirect("/")
        except sqlite3.IntegrityError:
            flash("That username is already taken. Try another.")

    return render_template_string(
        AUTH_TEMPLATE,
        title="Create Account",
        button_text="Sign Up",
        is_login=False,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"].strip()

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT id, password_hash, salt FROM users WHERE username = ?",
            (username,),
        )
        row = c.fetchone()
        conn.close()

        if row:
            user_id, stored_hash, salt = row
            computed_hash, _ = hash_password(password, salt)
            if computed_hash == stored_hash:
                session.permanent = True
                session["user_id"] = user_id
                session["username"] = username
                return redirect("/")

        flash("Invalid username or password.")

    return render_template_string(
        AUTH_TEMPLATE, title="Log In", button_text="Log In", is_login=True
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --- Main Dashboard Engine ---
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    display_curr = request.args.get("curr", "INR").upper()
    if display_curr not in ["INR", "USD"]:
        display_curr = "INR"

    sym = "₹" if display_curr == "INR" else "$"
    usd_rate = get_usd_to_inr_rate()

    # Load trades strictly for the logged-in user in chronological order
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT id, ticker, transaction_type, shares, price, trade_date, is_active 
        FROM transactions 
        WHERE user_id = ? 
        ORDER BY trade_date ASC, id ASC
    """,
        (user_id,),
    )
    raw_rows = c.fetchall()
    conn.close()

    raw_transactions = [
        {
            "id": r[0],
            "ticker": r[1],
            "type": r[2] if r[2] else "BUY",
            "shares": float(r[3]),
            "price": float(r[4]),
            "trade_date": r[5],
            "is_active": r[6],
        }
        for r in raw_rows
    ]

    # Inventory & Realized P&L Calculation (Weighted Average Cost Basis)
    portfolio_state = {}
    total_realized_pnl = 0.0

    for t in raw_transactions:
        ticker = t["ticker"]
        t_type = t["type"]
        sh = t["shares"]
        pr = t["price"]
        is_active = bool(t["is_active"])

        if ticker not in portfolio_state:
            portfolio_state[ticker] = {
                "shares": 0.0,
                "cost_basis": 0.0,
                "is_active": is_active,
            }

        if not is_active:
            portfolio_state[ticker]["is_active"] = False

        if t_type == "BUY":
            portfolio_state[ticker]["shares"] += sh
            portfolio_state[ticker]["cost_basis"] += sh * pr
        elif t_type == "SELL":
            curr_sh = portfolio_state[ticker]["shares"]
            curr_cost = portfolio_state[ticker]["cost_basis"]
            if curr_sh > 0:
                avg_cost = curr_cost / curr_sh
                sold_shares = min(sh, curr_sh)
                realized = (pr - avg_cost) * sold_shares
                if portfolio_state[ticker]["is_active"]:
                    total_realized_pnl += realized
                portfolio_state[ticker]["shares"] -= sold_shares
                portfolio_state[ticker]["cost_basis"] -= sold_shares * avg_cost

    holdings = []
    tot_inv = 0.0
    tot_cur = 0.0

    for ticker, data in portfolio_state.items():
        sh = data["shares"]
        cost = data["cost_basis"]
        avg = (cost / sh) if sh > 0 else 0.0
        is_active = data["is_active"]

        if round(sh, 4) <= 0:
            continue

        stock_info = fetch_stock_history(ticker)
        raw_price = stock_info["current_price"]
        if raw_price is None or raw_price <= 0:
            raw_price = avg

        asset_curr = stock_info["currency"]
        converted_price = raw_price
        if asset_curr == "USD" and display_curr == "INR":
            converted_price = raw_price * usd_rate
        elif asset_curr == "INR" and display_curr == "USD":
            converted_price = raw_price / usd_rate

        val = sh * converted_price
        pnl = val - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        if is_active:
            tot_inv += cost
            tot_cur += val

        history = stock_info["history"]
        rsi = compute_rsi(history)
        if rsi is not None:
            if rsi > 70:
                rsi_mood = "Overextended"
            elif rsi < 30:
                rsi_mood = "Oversold"
            else:
                rsi_mood = "Neutral"
        else:
            rsi_mood = "Neutral"

        sma200 = compute_sma(history, 200)
        price_vs_sma = (
            ((raw_price - sma200) / sma200 * 100) if sma200 > 0 else 0.0
        )
        vol = compute_annualized_volatility(history)

        holdings.append(
            {
                "ticker": ticker,
                "shares": round(sh, 4),
                "avg_price": avg,
                "current_price": converted_price,
                "current_val": val,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "rsi": rsi if rsi is not None else "--",
                "rsi_mood": rsi_mood,
                "price_vs_sma": round(price_vs_sma, 1),
                "vol": vol,
                "is_active": is_active,
                "weight": 0.0,
            }
        )

    portfolio_vol = 0.18
    daily_var = 0.0
    max_weight = 0.0
    total_unrealized_pnl = tot_cur - tot_inv
    portfolio_return_pct = (
        (total_unrealized_pnl / tot_inv * 100) if tot_inv > 0 else 0.0
    )

    if tot_cur > 0:
        weighted_vol = 0.0
        for h in holdings:
            if h["is_active"]:
                h["weight"] = round((h["current_val"] / tot_cur) * 100, 1)
                weighted_vol += (h["weight"] / 100.0) * h["vol"]
                if h["weight"] > max_weight:
                    max_weight = h["weight"]
            else:
                h["weight"] = 0.0

        portfolio_vol = weighted_vol
        daily_std = portfolio_vol / math.sqrt(252)
        daily_var = 1.645 * daily_std * tot_cur

    if max_weight > 45:
        conc_label = "High Concentration"
    elif max_weight > 25:
        conc_label = "Moderate Concentration"
    else:
        conc_label = "Well Diversified"

    benchmark_data = fetch_benchmark_return(display_curr)
    alpha = portfolio_return_pct - benchmark_data["return_pct"]

    mc_dict = run_monte_carlo(
        tot_cur, weighted_vol=portfolio_vol, weighted_mu=0.10
    )
    active_holdings = [h for h in holdings if h["is_active"]]

    display_ledger = sorted(
        raw_transactions, key=lambda x: (x["trade_date"], x["id"]), reverse=True
    )

    return render_template_string(
        DASHBOARD_TEMPLATE,
        user={"username": session["username"]},
        holdings=holdings,
        raw_transactions=display_ledger,
        total_invested=tot_inv,
        total_current=tot_cur,
        total_pnl=total_unrealized_pnl,
        total_realized=total_realized_pnl,
        benchmark=benchmark_data,
        alpha=alpha,
        portfolio_vol=portfolio_vol,
        daily_var=daily_var,
        max_weight=max_weight,
        conc_label=conc_label,
        mc_labels=json.dumps(mc_dict["labels"]),
        mc_median=json.dumps(mc_dict["median"]),
        mc_p10=json.dumps(mc_dict["p10"]),
        mc_p90=json.dumps(mc_dict["p90"]),
        chart_labels=json.dumps([h["ticker"] for h in active_holdings]),
        chart_values=json.dumps(
            [round(h["current_val"], 2) for h in active_holdings]
        ),
        display_curr=display_curr,
        sym=sym,
        today=str(date.today()),
    )


# --- Add Transaction (Supports BUY and SELL) ---
@app.route("/add", methods=["POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect("/login")

    display_curr = request.args.get("curr", "INR")
    tx_type = request.form.get("transaction_type", "BUY").upper()
    ticker = request.form["ticker"].strip().upper()
    shares = float(request.form["shares"])
    price = float(request.form["price"])
    trade_date = request.form["trade_date"]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO transactions (user_id, ticker, transaction_type, shares, price, trade_date, is_active) 
        VALUES (?, ?, ?, ?, ?, ?, 1)
    """,
        (session["user_id"], ticker, tx_type, shares, price, trade_date),
    )
    conn.commit()
    conn.close()
    return redirect(f"/?curr={display_curr}")


# --- Delete Single Transaction ---
@app.route("/delete/<int:tx_id>", methods=["POST"])
def delete_transaction(tx_id):
    if "user_id" not in session:
        return redirect("/login")

    display_curr = request.args.get("curr", "INR")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "DELETE FROM transactions WHERE id = ? AND user_id = ?",
        (tx_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return redirect(f"/?curr={display_curr}")


# --- What-If Toggle Active State ---
@app.route("/toggle/<ticker>", methods=["POST"])
def toggle_holding(ticker):
    if "user_id" not in session:
        return redirect("/login")

    display_curr = request.args.get("curr", "INR")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT is_active FROM transactions WHERE ticker = ? AND user_id = ? LIMIT 1",
        (ticker, session["user_id"]),
    )
    row = c.fetchone()
    if row:
        new_state = 0 if row[0] == 1 else 1
        c.execute(
            "UPDATE transactions SET is_active = ? WHERE ticker = ? AND user_id = ?",
            (new_state, ticker, session["user_id"]),
        )
        conn.commit()
    conn.close()
    return redirect(f"/?curr={display_curr}")


# --- CSV Export & Import Routes ---
@app.route("/export-csv")
def export_csv():
    if "user_id" not in session:
        return redirect("/login")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT ticker, transaction_type, shares, price, trade_date 
        FROM transactions 
        WHERE user_id = ? 
        ORDER BY trade_date ASC
    """,
        (session["user_id"],),
    )
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ticker", "transaction_type", "shares", "price", "trade_date"])
    for r in rows:
        writer.writerow(r)

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={session['username']}_portfolio.csv"
        },
    )


@app.route("/import-csv", methods=["POST"])
def import_csv():
    if "user_id" not in session:
        return redirect("/login")

    display_curr = request.args.get("curr", "INR")
    file = request.files.get("csv_file")
    if file:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for row in reader:
            try:
                ticker = row["ticker"].strip().upper()
                tx_type = row.get("transaction_type", "BUY").strip().upper()
                shares = float(row["shares"])
                price = float(row["price"])
                trade_date = row.get("trade_date", str(date.today()))

                c.execute(
                    """
                    INSERT INTO transactions (user_id, ticker, transaction_type, shares, price, trade_date, is_active) 
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                    (
                        session["user_id"],
                        ticker,
                        tx_type,
                        shares,
                        price,
                        trade_date,
                    ),
                )
            except Exception:
                continue
        conn.commit()
        conn.close()

    return redirect(f"/?curr={display_curr}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8501))
    app.run(host="0.0.0.0", port=port, debug=True)