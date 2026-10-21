FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/generated/src:/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY marketplace_impl ./marketplace_impl
CMD ["uvicorn", "marketplace_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
