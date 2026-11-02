FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --timeout=120 --retries=10 \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt
COPY *.py .
CMD ["python", "bot.py"]