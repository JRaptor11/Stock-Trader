# Algorithmic Trading Bot (FastAPI + Alpaca)

![Python](https://img.shields.io/badge/python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/framework-FastAPI-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

A modular algorithmic trading system built with FastAPI and Alpaca that performs real-time market data streaming, strategy-based signal evaluation, and automated paper trading with built-in risk management and runtime configuration via API endpoints.

## Architecture Overview

The system consists of a FastAPI backend, a threaded Alpaca market data stream, a modular strategy engine for evaluating trading signals, and background monitoring services that enforce risk management rules and system health checks.

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
│          Routers: /production /auth /test /config           │
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



## Features



- FastAPI-based backend with structured startup/shutdown lifecycle  

- Alpaca trading integration (paper trading mode)  

- Threaded market data streaming  

- Modular strategy engine with confidence scoring  

- Risk management fail-safes and trade rate limits  

- Runtime configuration management via API routes  

- Telegram bot integration for alerts and commands  

- Email alerts for important events  

- Trade logging and system lifecycle tracking  



---



## Technology Stack



- Python

- FastAPI

- Alpaca Trading API

- AsyncIO

- Threading

- REST API design

- Environment-based configuration



---



## Project Structure



```
app/

main.py # Application startup and lifecycle management



routes/

auth_routes.py # Authentication and token handling

production_routes.py # Production API routes

test_routes.py # Test and debugging routes

config_routes.py # Runtime configuration endpoints



utils/

config.py # Shared configuration values

alerts_utils.py # Email alerts

telegram_bot_utils.py # Telegram bot startup

logging_utils.py # Logging configuration

threading_utils.py # Safe thread helpers

trade_utils.py # Trade logging utilities

lifecycle_utils.py # Startup/shutdown tracking



stream.py # Alpaca data stream manager

strategy.py # Trading strategies and confidence scoring

state.py # Global application state container

constants.py # Risk limits and config defaults
```


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



### Fail-Safes



Multiple protections are implemented:



- Maximum equity loss

- Maximum position loss

- Trade rate limits

- Connection error monitoring

- Resource monitoring (CPU / memory)



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



Possible future enhancements include:



- Database-backed trade history

- Webhook-based order fill tracking

- Advanced strategy backtesting

- Live dashboard visualization

- Improved authentication system



---



## License



This project is provided for educational and portfolio demonstration purposes.

