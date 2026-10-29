FROM node:22-bookworm-slim AS verify_dashboard
WORKDIR /src
COPY macd_radar/index.html /src/macd_radar/index.html
COPY scripts/check_macd_dashboard.cjs /src/scripts/check_macd_dashboard.cjs
RUN node scripts/check_macd_dashboard.cjs && printf 'verified' > /ui-check-ok

FROM python:3.12-slim
WORKDIR /app
COPY . /app
COPY --from=verify_dashboard /ui-check-ok /ui-check-ok
RUN python -m compileall -q macd_radar run.py && \
    python -m unittest discover -s macd_radar/tests -p 'test_*.py' -v
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000
CMD ["python", "run.py"]
