FROM python:3.12-slim AS assembled

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . /app
# The tested synchronization module must be present in the served HTML.
# Fail the image build on missing/duplicated integration hooks.
RUN python -m unittest discover -s tests -p test_build_preflight_sync.py -v \
    && python scripts/build_preflight_sync.py

FROM node:22-bookworm-slim AS verified-ui
WORKDIR /checks
COPY --from=assembled /app/radar/static /checks/radar/static
COPY --from=assembled /app/tests/test_preflight_sync.cjs /checks/tests/test_preflight_sync.cjs
COPY --from=assembled /app/scripts/check_dashboard_js.cjs /checks/scripts/check_dashboard_js.cjs
RUN node tests/test_preflight_sync.cjs && node scripts/check_dashboard_js.cjs

FROM assembled AS runtime
# This dependency ensures the verification stage cannot be skipped.
COPY --from=verified-ui /checks/radar/static/pages.html /app/radar/static/pages.html
RUN cp config.example.json config.json && mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "run.py", "--serve"]
