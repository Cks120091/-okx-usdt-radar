FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN python -m compileall -q solana_paper run.py && \
    python -m unittest discover -s solana_paper/tests -p 'test_*.py' -v && \
    python -m unittest discover -s tests -p 'test_solana_paper.py' -v && \
    mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV PAPER_AUTORUN=1
EXPOSE 8000
CMD ["python", "-m", "solana_paper.app"]
