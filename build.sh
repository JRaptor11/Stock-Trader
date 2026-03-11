#!/bin/bash

# Install dependencies
pip install -r requirements.txt

# Confirm Flask and Uvicorn are installed
python -c "import flask; print('✅ Flask version:', flask.__version__)"
python -c "import fastapi; print('✅ FastAPI is ready.')"
python -c "import uvicorn; print('✅ Uvicorn version:', uvicorn.__version__)"

# Ensure this script stays executable
chmod +x build.sh
