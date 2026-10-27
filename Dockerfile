FROM node:22-bookworm-slim AS verified_ui
WORKDIR /app
COPY solana_paper/dashboard.html /app/solana_paper/dashboard.html
COPY solana_paper/retire_service_worker.js /app/solana_paper/retire_service_worker.js
COPY scripts/check_solana_dashboard.cjs /app/scripts/check_solana_dashboard.cjs
RUN node scripts/check_solana_dashboard.cjs && printf ok > /ui-check-ok

FROM python:3.12-slim
WORKDIR /app
COPY . /app
# Build must depend on the JS verifier, or Render could publish a broken page.
COPY --from=verified_ui /ui-check-ok /ui-check-ok
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN python -m compileall -q solana_paper run.py && \
    python -m unittest discover -s solana_paper/tests -p 'test_*.py' -v && \
    python -m unittest discover -s tests -p 'test_solana_paper.py' -v && \
    python -m unittest discover -s tests -p 'test_intelligence.py' -v && \
    python -m unittest discover -s tests -p 'test_openai_agent.py' -v && \
    python -m unittest discover -s tests -p 'test_shadow_judge.py' -v && \
    python -m unittest discover -s tests -p 'test_chain_stream.py' -v && \
    mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV PAPER_AUTORUN=1
EXPOSE 8000
CMD ["python", "-m", "solana_paper.app"]
