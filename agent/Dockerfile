FROM python:3.12-slim

WORKDIR /app
# Build context: repository root (see deploy.py)
COPY agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ ./agent/

ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
