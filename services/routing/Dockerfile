FROM python:3.12-slim

# Geospatial stack (GEOS/GDAL) for OSMnx / GeoPandas in slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        g++ \
        libgeos-dev \
        libproj-dev \
        proj-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py graph_service.py route_metrics.py ./

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}"]
