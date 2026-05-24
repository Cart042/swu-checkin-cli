FROM python:3.10-slim

WORKDIR /app

# Install OpenCV and system libraries needed by ddddocr
RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Default run command
CMD ["python", "check_in.py"]
