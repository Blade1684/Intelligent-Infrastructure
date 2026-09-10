FROM python:3.13-slim

WORKDIR /app

# Install stress-ng for controlled infrastructure
# anomaly demonstrations.
RUN apt-get update && \
    apt-get install -y --no-install-recommends stress-ng && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
