# main.py
# This version is modified to use uvicorn instead of gunicorn + gevent
# Uvicorn allows direct async handling and works well for local or dev environments

import os
import logging

from dotenv import load_dotenv

from app_instance import app
from core.app_lifespan import lifespan
from core.app_setup import configure_fastapi_app


load_dotenv()

app.router.lifespan_context = lifespan

configure_fastapi_app(app)


if __name__ == "__main__":
    import uvicorn

    port_env = os.environ.get("PORT", "8000")
    try:
        port = int(port_env)
    except ValueError:
        logging.warning("Invalid PORT env var %r. Using default port 8000.", port_env)
        port = 8000

    logging.info(f"🚀 Starting FastAPI server on port %s...", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)