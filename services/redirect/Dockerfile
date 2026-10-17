FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 app && mkdir -p /var/spool/qr && chown app /var/spool/qr
USER app
ENV SPOOL_DIR=/var/spool/qr
EXPOSE 8080
# uvloop + httptools são usados automaticamente pelo uvicorn[standard]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", \
     "--no-access-log", "--timeout-graceful-shutdown", "10"]
