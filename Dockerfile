FROM debian:12-slim AS build

# Install python
RUN apt-get update && \
    apt-get install --no-install-suggests --no-install-recommends --yes python3

# Add uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install python dependencies
ENV UV_COMPILE_BYTECODE=1 \
    UV_PYTHON=python3.11 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked


FROM gcr.io/distroless/python3-debian12

# Copy venv and source code
COPY --from=build /.venv /.venv
COPY telegram_autoregexbot /app/telegram_autoregexbot

WORKDIR /app

# Set VERSION as env
ARG VERSION
ENV VERSION=${VERSION}
ARG BOT_VERSION
ENV BOT_VERSION=${BOT_VERSION}

ENTRYPOINT ["/.venv/bin/python3", "-m", "telegram_autoregexbot.autoregex"]
