Manufacturing CSV Gateway (API)

A high-performance FastAPI backend designed to parse, merge, and serve manufacturing data from localized CSV files to the frontend dashboard.

## 🚀 Quick Start
1. Prerequisites

    Python: Version 3.9 or higher.

    Pip: Python package manager.

2. Environment Setup
If you don't have Python installed:
    Download it from python.org.

    During installation, ensure you check the box "Add Python to PATH".
Verify installation:
    python --version
    pip --version

3. Installation
Navigate to the API project folder and install the required libraries:
    pip install fastapi uvicorn pydantic email-validator

4. Data Structure Requirement
The API expects a data/ directory in the root folder. Each production line must have its own sub-folder containing the CSV files:
    /data
        /TOK
            cycle_time.csv
            cavity_temperature_01.csv
            oee_availability.csv
        /TOF
            ...
        /reports
            rules.json

5. Running the API
Start the server using uvicorn:
    uvicorn main:app --reload --port 8000

API URL: http://localhost:8000
Interactive Docs: http://localhost:8000/docs (Swagger UI)

## Alerting Rules

The API manages a "Rules Engine" stored in data/reports/rules.json.

    GET /rules: List all active monitoring rules.

    POST /rules: Create a new alert threshold.

    DELETE /rules/{id}: Remove a rule.

