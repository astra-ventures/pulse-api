FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Pulse source (the actual nervous system modules)
COPY pulse/ ./pulse/

# Copy API server
COPY main.py .

# State directory for companion data
RUN mkdir -p /data/companions

ENV PULSE_DATA_DIR=/data/companions
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
