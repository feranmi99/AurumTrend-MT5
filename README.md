# AurumTrend MT5 🥇

> A highly resilient, fully automated XAUUSD (Gold) trend-following algorithmic trading system built for MetaTrader 5.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-00a67d)
![MetaTrader 5](https://img.shields.io/badge/MetaTrader_5-Supported-orange)
![Status](https://img.shields.io/badge/Status-Active_Development-success)

## 📖 Overview
AurumTrend is a high-performance algorithmic trading bot specifically calibrated for the extreme volatility of **XAUUSD (Gold)**. It is wrapped in an asynchronous **FastAPI** web server, allowing for real-time status monitoring, remote trade history inspection, and dynamic system tuning without ever needing to restart the core trading thread.

The strategy relies on capturing massive macroeconomic trends while aggressively filtering out sideways market noise, resulting in a historically validated mathematical edge on the M5 timeframe.

## ⚙️ Core Strategy Architecture
- **Timeframe**: `M5` (with `H1` Macro Filter)
- **Signal Generation**: Fast/Slow EMA Crossovers (e.g., 9/21).
- **Momentum Filter**: ADX (Average Directional Index) thresholding ensures the bot only enters during strong momentum phases.
- **Macro-Trend Alignment (HTF Filter)**: The bot dynamically fetches the H1 timeframe and computes the H1 EMA50. M5 signals are strictly rejected if they attempt to trade against the H1 macro trend.
- **Asymmetric Risk Management**: Utilizes an extremely wide initial stop-loss to survive Gold's aggressive "wicks" and stop-hunts, paired with an aggressive trailing stop that locks in profits once a trend is successfully caught. *No take-profit is used; winners are left to run indefinitely until the trend reverses.*

## 🛡️ Resilience & Safety Features
1. **Thread-Pool Isolation**: MT5 synchronous API calls and SQLite database writes are isolated in `asyncio.to_thread` pools, ensuring the FastAPI event loop never blocks or freezes during high network latency or disk I/O.
2. **Infinite Auto-Reconnect**: If the broker server goes down for weekend maintenance, the bot catches the `MT5Error`, pauses, and infinitely attempts background reconnections until the broker comes back online.
3. **Timezone Immunity**: Deal history is fetched via globally unique `position_ticket` IDs, entirely bypassing MT5 Broker Server Time vs. UTC timezone bugs.
4. **Weekend Gap Protection**: Automatically executes a Market Close on all open positions exactly at Friday 21:00 UTC to protect against disastrous Friday-to-Monday market gaps.
5. **Dynamic Slippage Control**: Bypasses broker requote rejections by passing configurable deviation points into the API order requests.

## 🤖 Self-Tuning Module
The algorithm includes a background `tuning.py` module. Every `N` closed trades, the bot analyzes its recent historical performance and calculates if raising or lowering the ADX Threshold would have improved the profit factor. It writes these tuning suggestions to the SQLite database, which can be reviewed and applied live via the FastAPI endpoints.

## 🚀 Installation & Usage

### Prerequisites
- **Windows OS** or a **Windows VPS** (MetaTrader 5 Python integration *only* works on Windows).
- **MetaTrader 5 Desktop Terminal** installed and logged into your broker.
- **Python 3.10+**

### Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/feranmi99/AurumTrend-MT5.git
   cd AurumTrend-MT5
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy the environment template and configure your broker details:
   ```bash
   cp .env.example .env
   ```
   *Edit `.env` and add your MT5 Account Number, Password, and Server.*

### Running the Bot
Start the FastAPI server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Once running, monitor the bot via your browser:
- **Status Dashboard**: `http://localhost:8000/status`
- **Trade History DB**: `http://localhost:8000/trades`

## ⚠️ Disclaimer
**This software is for educational and research purposes only.** Foreign exchange and CFD trading carries a high level of risk and may not be suitable for all investors. The past performance of this algorithm does not guarantee future results. **ALWAYS run this system on a Demo account for a minimum of 4 weeks (or 200 trades) to validate your broker's spread, latency, and slippage conditions before ever risking real capital.**
