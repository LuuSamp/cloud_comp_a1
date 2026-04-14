FROM python:3.12-slim

WORKDIR /app
# Build context: repository root (see deploy.py)
COPY services/ordering/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/ordering/ .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
