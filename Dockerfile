FROM python:3.11-slim

# Install ffmpeg for whisper (if needed later)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway sets PORT env var
CMD ["python", "server.py"]
