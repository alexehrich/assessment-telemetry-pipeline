# Agent and receiver share one image: stdlib-only Python, no dependencies to install.
FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pipeline /app/pipeline
ENTRYPOINT ["python3", "-m"]
CMD ["pipeline.agent"]
