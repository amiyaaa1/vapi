FROM python:3.11-slim

# 安装 Chromium 和必要的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libxshmfence1 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libatspi2.0-0 \
    xvfb \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium
RUN python -m patchright install chromium

# 复制源码
COPY . .

EXPOSE 5000

# 使用 xvfb-run 运行 (headless 需要虚拟显示)
CMD ["python", "api_solver.py", "--browser_type", "chromium", "--thread", "5"]
