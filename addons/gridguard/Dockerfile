# addons/gridguard/Dockerfile
FROM python:3.12-alpine
RUN apk add --no-cache openssl tzdata
WORKDIR /app
COPY gridguard/ ./gridguard/
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "gridguard.main"]
