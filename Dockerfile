FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . /app
RUN cp config.example.json config.json && mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "run.py", "--serve"]
