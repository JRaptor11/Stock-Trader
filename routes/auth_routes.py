# auth_routes.py

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from state import app_state
from utils import config_utils as config

auth_routes = APIRouter()
security = HTTPBasic()

# ================================================================
# AUTHENTICATION ROUTES
# ================================================================
#
# ┌──────────────────┬────────┬──────────────────────────────────┐
# │ Route            │ Method │ Description                      │
# ├──────────────────┼────────┼──────────────────────────────────┤
# │ /login           │ POST   │ Authenticate and return token    │
# │ /logout          │ POST   │ Invalidate auth token            │
# │ /verify-token    │ GET    │ Verify token validity            │
# └──────────────────┴────────┴──────────────────────────────────┘
#
# Notes
# ---------------------------------------------------------------
# • Uses HTTP Basic authentication
# • Generates session tokens
# • Tokens stored in app_state["routes"]["auth_routes"]["token_store"]
#
# ================================================================

# ================================================================
# AUTHENTICATION
# ================================================================

@auth_routes.post("/login")
def login(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Authenticate with HTTP Basic Auth and return a temporary token.
    """
    correct_username = config.HEALTH_USERNAME
    correct_password = config.HEALTH_PASSWORD

    if not correct_username or not correct_password:
        raise HTTPException(status_code=500, detail="Health credentials not configured")

    if (
        credentials.username != correct_username
        or credentials.password != correct_password
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = secrets.token_hex(16)
    app_state["routes"]["auth_routes"]["token_store"].add(token)

    return {
        "message": "Login successful",
        "token": token,
    }

# ================================================================
# TOKEN MANAGEMENT
# ================================================================

@auth_routes.post("/logout")
def logout(request: Request):
    """
    Invalidate an existing auth token.
    """
    token = request.headers.get("Authorization")

    if token in app_state["routes"]["auth_routes"]["token_store"]:
        app_state["routes"]["auth_routes"]["token_store"].remove(token)
        return {"message": "Logout successful"}

    raise HTTPException(status_code=401, detail="Invalid token")


@auth_routes.get("/verify-token")
def verify_token(request: Request):
    """
    Verify whether a provided auth token is still valid.
    """
    token = request.headers.get("Authorization")

    if token in app_state["routes"]["auth_routes"]["token_store"]:
        return {"message": "Token is valid"}

    raise HTTPException(status_code=401, detail="Invalid token")