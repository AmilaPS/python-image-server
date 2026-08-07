FROM python:3.11-slim

# System Level Cairo dependencies Install කිරීම (libgdk-pixbuf-2.0-0 Fix)
RUN apt-get update && apt-get install -y \
    libcairo2 \
    libcairo2-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Application Files Copy කිරීම
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Server Start Command
CMD ["sh", "-c", "uvicorn image_maker:app --host 0.0.0.0 --port ${PORT:-3001}"]
