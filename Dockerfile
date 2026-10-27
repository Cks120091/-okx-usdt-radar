FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN python -m compileall -q solana_paper run.py && \
    python -m unittest discover -s tests -p 'test_solana_paper.py' -v && \
    mkdir -p /app/data
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "run.py", "--serve"]
