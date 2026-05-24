FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

ENV PYTHONUNBUFFERED=1 \
    AUTO_MARKETPLACE_APP_DIR=/app \
    AUTO_MARKETPLACE_DATA_DIR=/data \
    AUTO_MARKETPLACE_NOVNC_DIR=/app/noVNC

WORKDIR /app

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-dejavu \
    fonts-liberation \
    openbox \
    wget \
    x11-utils \
    x11vnc \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN wget -qO- https://github.com/novnc/noVNC/archive/v1.4.0.tar.gz | tar xz \
    && mv noVNC-1.4.0 /app/noVNC \
    && wget -qO- https://github.com/novnc/websockify/archive/v0.11.0.tar.gz | tar xz \
    && mv websockify-0.11.0 /app/noVNC/utils/websockify

COPY . .

RUN chmod +x /app/start.sh \
    && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

CMD ["./start.sh"]
