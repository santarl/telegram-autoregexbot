# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools just in case (usually not needed for pure python, but good practice)
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project definition
COPY pyproject.toml README.md ./
COPY telegram_autoregexbot/ telegram_autoregexbot/

# Install dependencies into a temporary location
# We use --user to install to /root/.local, easy to copy later
RUN pip install --user --no-cache-dir .

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Keep Python from buffering stdout/stderr (logs appear immediately)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create a non-root user for security
RUN useradd -m -u 1000 botuser

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /root/.local /home/botuser/.local

# Update PATH to include user installed bin
ENV PATH=/home/botuser/.local/bin:$PATH

# Copy source code (needed for the module execution)
COPY telegram_autoregexbot/ telegram_autoregexbot/

# Switch to non-root user
USER botuser

# Command to run the bot
CMD ["python", "-m", "telegram_autoregexbot.autoregexbot"]