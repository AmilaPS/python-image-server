FROM python:3.11-slim

# Debian Trixie වලට ගැලපෙන පරිදි libgdk-pixbuf-xlib-2.0-0 භාවිතය
RUN apt-get update && apt-get install -y \
    libcairo2 \
    libcairo2-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn image_maker:app --host 0.0.0.0 --port ${PORT:-3001}"]
