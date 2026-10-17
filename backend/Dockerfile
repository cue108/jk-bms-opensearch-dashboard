# JK BMS → OpenSearch collector (BLE, read-only)
FROM python:3.12-slim

# Bluetooth note: bleak talks to the HOST's BlueZ over the system D-Bus
# socket, which is bind-mounted at runtime (see docker-compose.yml).
# No privileged mode and no host networking are required — docs/bluetooth.md.

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jkbms.py collector.py os_push.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8099
CMD ["python", "-u", "collector.py"]
