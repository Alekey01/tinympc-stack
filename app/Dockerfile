FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt==2.1.0
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 TZ=America/Mexico_City
