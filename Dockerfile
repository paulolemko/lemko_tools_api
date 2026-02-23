# =========================
# Dockerfile (CPU, slim)
# =========================
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MORPHOLOGY_STRUCTURE_FILE=/app/morphology_structure_pl_lem_eng/morphology_structure_lem01.json

# system libs dla soundfile/librosa/ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libsndfile1 ffmpeg git libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Zależności (bez cache pip w warstwie)
COPY requirements-prod.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install --no-cache-dir -r /app/requirements.txt

# Przygotuj katalogi, które będą później podpinane z hosta jako wolumeny
RUN mkdir -p \
    /app/scripts \
    /app/models \
    /app/psql_dump \
    /app/logs \
    /app/vocab_json \
    /app/morphology_structure_pl_lem_eng

# Konfiguracja przez ENV (wartości domyślne)
#ENV MODEL_PATH="/models/epoch6-step4571_CAPS_WER8.nemo" \
#    JWT_SECRET="" \
#    MAX_UPLOAD_MB="200" \
#    MAX_AUDIO_SECONDS="7200" \
#    CORS_ALLOW_ORIGINS="*" \
#    TRANS_DIR="/data/transkrypcje" \
#    LOG_PATH="/data/log.json" \
#    MAX_CONCURRENCY="1" \
#    BEAM_SIZE="8" \
#    MAX_SYMBOLS_PER_STEP="32"

EXPOSE 8000
#CMD ["uvicorn","--app-dir","/app/scripts","app:app","--host","0.0.0.0","--port","8000","--workers","2"]
CMD ["uvicorn","--app-dir","/app/scripts","app:app","--host","0.0.0.0","--port","8000"]
