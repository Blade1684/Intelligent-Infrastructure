FROM python:3.12-slim

WORKDIR /monitor

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "monitor.py"]