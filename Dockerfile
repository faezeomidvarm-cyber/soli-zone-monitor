FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY zone_monitor.py zones.json ./

RUN mkdir -p /app/data

CMD ["python", "-u", "zone_monitor.py", "--state", "/app/data/state.json", "--log", "/app/data/alerts.jsonl"]
