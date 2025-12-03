FROM python:3.11-slim

RUN apt update && apt install -y ffmpeg && apt clean

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY app.py /app/

RUN mkdir -p input output conf logs \
    && mkdir -p input/480 input/720 input/1080

ENV PYTHONUNBUFFERED=1

VOLUME ["/app/input", "/app/output", "/app/conf", "/app/logs"]

CMD ["python", "app.py"]
