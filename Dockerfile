FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HACKALEM_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY smartbuyer/ ./smartbuyer/
COPY web/ ./web/

USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "smartbuyer.serve"]
