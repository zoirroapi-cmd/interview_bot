FROM python:3.12-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY certs/russian_trusted_root_ca_pem.crt \
    /usr/local/share/ca-certificates/russian_trusted_root_ca.crt

RUN update-ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

CMD ["python", "-m", "app.main"]