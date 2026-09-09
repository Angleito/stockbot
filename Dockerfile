FROM python:3.14-slim
ARG BUN_VERSION=1.4.2
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
 && echo "36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913  /tmp/bun.zip" | sha256sum -c - \
 && unzip -q /tmp/bun.zip -d /tmp \
 && mv /tmp/bun-linux-x64/bun /usr/local/bin/bun \
 && chmod +x /usr/local/bin/bun \
 && rm -rf /tmp/bun.zip /tmp/bun-linux-x64
WORKDIR /app
COPY requirements.txt package.json bun.lock ./
RUN python -m venv /app/venv \
 && /app/venv/bin/pip install --upgrade pip \
 && /app/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && bun install \
 && printf '#!/bin/sh\nexec /usr/local/bin/bun /app/node_modules/.bin/pi "$@"\n' > /usr/local/bin/pi \
 && chmod +x /usr/local/bin/pi
COPY . .
ENV STOCKBOT_DATA_DIR=/data
ENV PATH="/app/venv/bin:$PATH"
RUN pi --help | grep -i builtin
