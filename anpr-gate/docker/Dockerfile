FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/anpr

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
RUN groupadd --gid 10001 anpr \
    && useradd --uid 10001 --gid 10001 --create-home anpr \
    && chown -R anpr:anpr /app /home/anpr

USER anpr

# Bake both ONNX assets into the image so production workers never need to
# download models during a restart or an internet outage.
RUN python -c "from app.anpr.recognizer import PlateRecognizer; PlateRecognizer()"

EXPOSE 8000

CMD ["python", "-m", "app.web"]
