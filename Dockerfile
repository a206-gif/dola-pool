FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOLA_DATA_DIR=/app/data \
    DOLA_OUTPUT_DIR=/app/data/output \
    DOLA_UPLOAD_DIR=/app/data/uploads \
    DOLA_DB=/app/data/dola.db

WORKDIR /app

# 依赖单独一层，改代码不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY desktop.py ./
COPY requirements*.txt ./
COPY gen/ ./gen/
COPY templates/ ./templates/
COPY static/ ./static/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY cleanup.sh README.md .env.example ./

RUN mkdir -p /app/data/output /app/data/uploads

EXPOSE 8000
VOLUME ["/app/data"]

# 默认演示模式，方便容器里先跑通；正式使用请通过环境变量 DOLA_DEMO=0 关闭
ENV DOLA_DEMO=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"

CMD ["python", "app.py"]
