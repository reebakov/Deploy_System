FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ansible iputils-ping openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# Один worker обязателен (см. README)
CMD ["uvicorn", "mm_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
