# the hosted web app. models run as services, see requirements-web.txt
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt \
    && python -m compileall -q /usr/local/lib/python3.12/site-packages
COPY . .

RUN useradd --create-home shop
USER shop
EXPOSE 10000
CMD ["sh", "-c", "uvicorn web.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers"]
