# Build a small, reproducible image for the bot.
FROM python:3.11-slim

# Avoid .pyc and buffered stdout so logs stream in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY polybot ./polybot
COPY run.py .
COPY config.example.yaml .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 polybot \
    && mkdir -p /app/state \
    && chown -R polybot:polybot /app
USER polybot

# Paper mode by default. Override the command to go live, e.g.:
#   docker run ... polybot python run.py run --live
CMD ["python", "run.py", "run"]
