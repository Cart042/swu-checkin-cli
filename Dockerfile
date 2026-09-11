FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=UTF-8 \
    TZ=Asia/Shanghai \
    SWU_CONFIG_DIR=/data

# Install OpenCV and system libraries needed by ddddocr
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies.  Keep pip's build cache out of the
# image; CI uses setup-python's cache instead.
COPY requirements.txt .
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

# The check-in code launches headless Chromium without a channel, so the
# smaller headless shell is sufficient.  --with-deps also installs the Linux
# libraries needed by the browser in the slim base image.
RUN python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy script files
COPY . .
RUN mkdir -p /data
VOLUME ["/data"]

# Default run command
CMD ["python", "check_in.py"]
