from utils import config_utils as config
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = config.HEALTH_USERNAME
    correct_password = config.HEALTH_PASSWORD

    if not correct_username or not correct_password:
        raise HTTPException(status_code=500, detail="Health credentials not configured")

    if credentials.username != correct_username or credentials.password != correct_password:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return credentials.username