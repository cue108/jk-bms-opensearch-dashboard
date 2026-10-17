# JK BMS web frontend (status page + history charts)
FROM python:3.12-slim

# No dependencies: Python standard library only; uPlot is vendored in web/.

WORKDIR /app
COPY server.py .
COPY web/ web/

ENV PYTHONUNBUFFERED=1
EXPOSE 8098
CMD ["python", "-u", "server.py"]
