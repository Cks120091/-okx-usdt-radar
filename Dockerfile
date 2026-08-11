FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN cp config.example.json config.json && mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "run.py", "--serve"]

