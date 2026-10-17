FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker ./worker
RUN useradd -r -u 10001 app
USER app
# Monte o GeoLite2-City.mmdb (MaxMind) em /data para geolocalização por cidade
CMD ["python", "-m", "worker.main"]
