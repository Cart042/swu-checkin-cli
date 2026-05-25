FROM python:3.10-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    TZ=Asia/Shanghai \
    SWU_CONFIG_DIR=/data

# Install OpenCV and system libraries needed by ddddocr
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install chromium and its system dependencies for Playwright
RUN playwright install --with-deps chromium

# Copy script files
COPY . .
RUN mkdir -p /data
VOLUME ["/data"]

# Default run command
CMD ["python", "check_in.py"]
