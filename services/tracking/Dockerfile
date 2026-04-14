FROM python:3.12-slim

WORKDIR /app
# Build context: repository root (see deploy.py)
COPY services/tracking/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY services/tracking/main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
