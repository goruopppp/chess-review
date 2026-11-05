FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY server/ server/
COPY web/ web/
COPY samples/ samples/

# engines/: fetched at build time; falls back to Debian's stockfish package
# (older version, fully functional) if GitHub's CDN is unreachable.
RUN mkdir -p engines data \
 && ( python download_stockfish.py && rm download_stockfish.py ) \
 || ( echo "GitHub download unreachable - using Debian stockfish package instead" \
      && rm -f download_stockfish.py \
      && apt-get update -qq \
      && apt-get install -y --no-install-recommends stockfish \
      && cp "$(find /usr -name stockfish -type f | head -n 1)" engines/stockfish ) \
 && chmod +x engines/stockfish \
 && ls -la engines/

# HOST/PORT are consumed by run.py
ENV HOST=0.0.0.0 PORT=7860
EXPOSE 7860

CMD ["python", "run.py"]
