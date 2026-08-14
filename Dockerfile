FROM python:3.12-slim

LABEL org.opencontainers.image.title="LLazarus" \
      org.opencontainers.image.description="Wake-on-demand routing for local AI inference"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_DATA=/data

WORKDIR /app

EXPOSE 4000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       iputils-ping \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.example.yml .

RUN mkdir -p /data

CMD ["python", "-m", "app.main"]
