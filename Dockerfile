FROM python:3.12-alpine

RUN adduser -D monitor
USER monitor
WORKDIR /app

COPY monitor.py .

CMD ["python3", "-u", "monitor.py"]
