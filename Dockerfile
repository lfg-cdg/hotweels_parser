FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 bot && mkdir -p /app/data && chown bot /app/data
USER bot
VOLUME ["/app/data"]
CMD ["python", "-m", "app.main"]
