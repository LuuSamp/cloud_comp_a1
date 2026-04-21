FROM python:3.12-slim

WORKDIR /app
COPY simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY simulator/ simulator/

CMD ["python", "-m", "simulator.services.customer_simulation_service"]
