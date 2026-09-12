FROM python:3.14-slim

WORKDIR /app
# PLAYWRIGHT_BROWSERS_PATH 必须指向共享路径：容器以非 root 用户运行，
# 浏览器若装在 root 的 ~/.cache 下，运行用户会找不到 Chromium。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=UTF-8 \
    TZ=Asia/Shanghai \
    SWU_CONFIG_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

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

# 非 root 运行用户。容器内长期保存账号、Token 缓存、日志和运行锁，
# 以 root 运行会让这些凭据落在可被容器内任意进程读取的位置。
RUN groupadd --gid 10001 swu \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin swu \
    && chmod -R a+rX /opt/ms-playwright

# Copy script files
COPY --chown=10001:10001 . .
RUN mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data

USER 10001:10001
VOLUME ["/data"]

# Default run command
CMD ["python", "check_in.py"]
