FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY server/ server/
COPY web/ web/
COPY samples/ samples/

# engines/: fetched at build time (kept out of the repo)
RUN mkdir -p engines data \
 && python download_stockfish.py \
 && rm download_stockfish.py \
 && chmod +x engines/stockfish

# HOST/PORT are consumed by run.py
ENV HOST=0.0.0.0 PORT=7860
EXPOSE 7860

CMD ["python", "run.py"]
