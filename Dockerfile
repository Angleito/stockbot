FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://bun.sh/install | bash \
 && mv /root/.bun/bin/bun /usr/local/bin/bun
WORKDIR /app
COPY requirements.txt package.json bun.lock ./
RUN pip install --no-cache-dir -r requirements.txt \
 && bun install \
 && ln -sf /app/node_modules/.bin/pi /usr/local/bin/pi
COPY . .
ENV STOCKBOT_DATA_DIR=/data
RUN pi --help | grep -i builtin
