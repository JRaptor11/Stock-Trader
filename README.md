# Algorithmic Trading Bot (FastAPI + Alpaca)

![Python](https://img.shields.io/badge/python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/framework-FastAPI-green)
![Trading API](https://img.shields.io/badge/API-Alpaca-orange)
![Paper Trading](https://img.shields.io/badge/trading-paper--mode-blue)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

A modular algorithmic trading system built with **FastAPI** and the **Alpaca Trading API** that performs real-time market data streaming, evaluates trading signals through a **confidence-weighted strategy engine**, and executes automated paper trades with built-in risk management and monitoring.

The system emphasizes **reliability, observability, and modular architecture**, making it suitable as a foundation for experimenting with algorithmic trading strategies and distributed trading system design.

## Architecture Overview

The system consists of a FastAPI backend, a threaded Alpaca market data stream, a modular strategy engine for evaluating trading signals, and background monitoring services that enforce risk management rules and system health checks.

---

## Design Principles

This project was built with several core engineering principles:

### Reliability
The system is designed to run continuously while maintaining safe behavior under unexpected conditions.

Key mechanisms include:

- crash-protected thread execution
- automatic stream reconnection
- heartbeat monitoring for background services
- graceful shutdown handling
- resource usage monitoring

### Modularity
Trading strategies are implemented as independent components.  
New strategies can be added without modifying the core trading engine.

### Observability
The system exposes detailed runtime state through:

- monitoring endpoints
- structured logging
- trade decision logging
- diagnostic routes

---

## System Architecture

```
                 ┌──────────────────────────┐
                 │        Client/User       │
                 │  (Swagger UI / scripts)  │
                 └─────────────┬────────────┘
                               │ HTTP
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                         FastAPI App                         │
│                     (lifespan startup)                      │
│                                                             │
│            Routers: admin / auth / dev / public             │
│                                                             │
│  ┌───────────────────────┐  ┌────────────────────────────┐  │
│  │    Strategy Engine    │  │    Background Services     │  │
│  │ (signals/confidence)  │  │   positions/balance/risk   │  │
│  └───────────┬───────────┘  └─────────────┬──────────────┘  │
│              │                            │                 │
│              ▼                            ▼                 │
│            Trade Decisions / Fail-Safe Monitor              │
│              │                            │                 │
└──────────────┼────────────────────────────┼─────────────────┘
               │                            │
               ▼                            ▼
    ┌─────────────────────┐    ┌─────────────────────────┐
    │   Threaded Alpaca   │    │ Alerts (Email/Telegram) │
    │ Market Data Stream  │    └─────────────────────────┘
    └──────────┬──────────┘
               │
               ▼
      ┌─────────────────┐
      │   Alpaca API    │
      │ (data + trade)  │
      └─────────────────┘
```

**Notes**
- The **lifespan startup** initializes config, the Alpaca TradingClient, the threaded data stream, and background tasks.
- Strategy signals are combined into a **confidence score** and routed through risk controls before orders are placed.
- Alerts notify on startup/shutdown and important state changes (Telegram + email).

⚠️ **Disclaimer:**  

This project is for educational and portfolio purposes only. It is not financial advice and should not be used for live trading without proper testing and risk evaluation.

---

## Key Components

### FastAPI Application
Provides the HTTP interface for monitoring, configuration, and administrative control.

### Threaded Market Data Stream
A dedicated Alpaca websocket stream processes live trade data and feeds it into the strategy engine.

### Strategy Engine
Evaluates market conditions using multiple strategies and produces a confidence-based trading signal.

### Background Services
Several monitoring services run continuously:

- Position tracking
- Account balance monitoring
- Market hours tracking
- Performance monitoring

### Alert System
Important events trigger notifications through:

- Email alerts
- Telegram bot integration

---



## Features

- FastAPI backend with structured startup and shutdown lifecycle  
- Real-time Alpaca market data streaming  
- Modular strategy engine with confidence-based signal evaluation  
- Adaptive strategy weighting based on market volatility  
- Automated paper trading with order reconciliation protection  
- Built-in risk management and fail-safe protections  
- Runtime configuration updates via API endpoints  
- Background monitoring services for positions, balances, and system health  
- Email and Telegram alert integration  
- Trade logging and lifecycle event tracking



---

## Technology Stack

Core technologies used in the system:

- **Python** — primary programming language
- **FastAPI** — high-performance async API framework
- **Alpaca Trading API** — market data streaming and order execution
- **AsyncIO** — asynchronous task management
- **Threading** — isolated streaming and monitoring services
- **REST API design** — operational monitoring and configuration endpoints
- **Environment-based configuration** — secure runtime configuration management

---



## Project Structure



```
app/

main.py
Application startup and lifecycle management

routes/
admin_routes.py
auth_routes.py
dev_routes.py
public_routes.py

services/
Background monitoring services (positions, performance, market hours)

stream.py
Threaded Alpaca market data stream manager

strategy.py
Trading strategies and confidence evaluation

state.py
Global application state container

constants.py
Risk limits and configuration defaults

utils/
alerts_utils.py
auth_utils.py
config_utils.py
logging_utils.py
misc_utils.py
threading_utils.py
trade_utils.py
lifecycle_utils.py
```


---


## Strategy Decision Logging

The trading engine logs detailed information about each trade evaluation.

Important log fields include:

| Field | Description |
|------|-------------|
| decision_stage | Which phase of the evaluation pipeline produced the decision |
| decision_reason | Explanation for the final decision |
| buy_edge | Positive confidence contribution for a buy |
| sell_edge | Negative confidence contribution for a sell |

These logs make it possible to analyze why trades occurred and to debug strategy behavior.

---

## Example Trade Decision Log

Example log entry produced during strategy evaluation:

[HandleTrade] symbol=AAPL price=184.23
[StrategyManager] buy_conf=0.62 sell_conf=-0.14 volatility=low
[ExecutionCheck] has_position=False cooldown=False
[OrderExecutor] submitting BUY order qty=10 limit_price=184.25

These logs allow developers to trace exactly how the strategy engine reached a trading decision.

---

## How It Works



### Application Startup



When the FastAPI application starts:



1. Environment variables are loaded.

2. The Alpaca TradingClient is initialized.

3. The data stream connection to Alpaca is started.

4. Background services begin monitoring:

&nbsp;  - Positions

&nbsp;  - Account balance

&nbsp;  - Fail-safes

5. Telegram alert bot is started.

6. Runtime configuration defaults are loaded.



---



### Strategy Evaluation



Trading strategies produce signals that are combined into a **confidence score**.



Signals are categorized as:



- Fast signals

- Lagging confirmation signals

- Brakes / filters



The bot adapts strategy weighting based on market volatility.

---

### Trading Decision Flow

Each incoming trade tick follows the pipeline below:

1. **Market Data Stream**
   - Alpaca websocket delivers a trade event.

2. **Market Data Buffer**
   - The system stores recent price and volume history per symbol.

3. **Strategy Evaluation**
   - Multiple strategies analyze the updated market data.

4. **Confidence Model**
   - Signals are combined into a weighted buy/sell confidence score.

5. **Risk Controls**
   - Fail-safe checks validate the trade decision.

6. **Order Execution**
   - A buy or sell order is submitted through the Alpaca Trading API.

7. **State Update & Alerts**
   - Trade state is logged and alerts are sent when appropriate.

---

### Risk Management & Fail-Safes

Multiple protections ensure the system behaves safely during abnormal market conditions or technical failures.

Implemented safeguards include:

- maximum account equity loss threshold
- per-position loss limits
- trade rate limiting
- connection failure monitoring
- resource usage alerts (CPU and memory)
- order reconciliation to prevent duplicate orders

These mechanisms ensure the system avoids runaway trading behavior and maintains predictable operation.

---



## Running the Project


### 1️⃣ Install Dependencies



Create a virtual environment:



```bash
python -m venv venv
```



Activate it:



Windows:



```bash
venv\Scripts\activate
```



Mac/Linux:



```bash
source venv/bin/activate
```



Install dependencies:



```bash
pip install -r requirements.txt
```



---



### 2️⃣ Create an Environment File



Create a `.env` file in the project root.



Example:




```
API_KEY=your_alpaca_api_key
SECRET_KEY=your_alpaca_secret_key
ALPACA_URL=https://paper-api.alpaca.markets

SYMBOL=AAPL
ENV=development
PORT=8000

EMAIL_ADDRESS=example@email.com
EMAIL_PASSWORD=your_email_password
EMAIL_RECIPIENTS=example@email.com

TELEGRAM_BOT_TOKEN=your_telegram_token
TELEGRAM_CHAT_ID=123456789

HEALTH_USERNAME=admin
HEALTH_PASSWORD=change_me
```


⚠️ Never commit `.env` files to GitHub.



---



### 3️⃣ Run the Application



```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```



Or run directly:



```bash
python app/main.py
```



---

### API Response Examples

### Monitoring Endpoints

The system exposes endpoints for operational monitoring:

| Endpoint | Purpose |
|--------|--------|
| `/healthz` | Service health check |
| `/stream-status` | Alpaca stream state and connection stats |
| `/metrics` | CPU and memory usage |
| `/uptime-health` | Lightweight uptime check |

#### Health Endpoint

Example response from the `/health` endpoint:

```json
{
  "status": "running",
  "stream": "connected",
  "positions": 0,
  "account_equity": 100000,
  "symbol": "AAPL"
}
```

#### Positions Endpoint

Example response from the `/positions` endpoint:

```json
{
  "positions": [
    {
      "symbol": "AAPL",
      "qty": 10,
      "avg_entry_price": 182.34,
      "market_price": 183.02,
      "unrealized_pl": 6.80
    }
  ],
  "total_positions": 1
}
```

This endpoint returns currently held positions and their unrealized profit/loss.

#### Stream Status Endpoint

Example response from `/stream-status`:

```json
{
  "stream_running": true,
  "connected": true,
  "symbols": ["AAPL"],
  "last_trade_update": "2026-03-04T15:31:12Z"
}
```

This endpoint reports the health of the Alpaca market data stream and confirms the system is receiving real-time updates.



Once running, open:

http://localhost:8000/docs

to explore the API endpoints through FastAPI's interactive documentation.


---


## API Documentation



Once running locally, visit:



Swagger UI:





http://localhost:8000/docs





ReDoc:





http://localhost:8000/redoc





---



## Security Notes



This repository uses environment variables for sensitive credentials.



Before publishing the repository:



- API keys are removed from source code

- `.env` files are ignored via `.gitignore`

- Secrets are loaded at runtime only



---



## Future Improvements

Planned enhancements include:

- database-backed trade history storage
- webhook-based order fill tracking
- strategy backtesting framework
- live trading dashboard for monitoring
- multi-symbol portfolio trading
- advanced performance analytics



---



## License



This project is provided for educational and portfolio demonstration purposes.

