# Treadwell Customer Proposal Portal — single FastAPI container serving the API
# and the static customer frontend.
FROM python:3.11-slim

WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

EXPOSE 8898
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8898"]
