FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN mkdir -p /app/data && python -m compileall -q solana_paper
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV PAPER_AUTORUN=1
EXPOSE 8000
# The old OKX radar is not run by this image.
CMD ["python", "-m", "solana_paper.app"]
