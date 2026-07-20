# code-flow workflow engine
FROM python:3.12-slim

WORKDIR /app

# install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# the app itself — NOTE: workflows/, environments/ and history/ are NOT
# baked into the image; they are volume-mounted (see docker-compose.yml)
COPY engine/ ./engine/
COPY app.py ui.html ./

# where the mounted folders will appear inside the container
ENV CODEFLOW_WORKFLOWS_DIR=/data/workflows \
    CODEFLOW_ENVIRONMENTS_DIR=/data/environments \
    CODEFLOW_HISTORY_DIR=/data/history \
    CODEFLOW_HOST=0.0.0.0 \
    CODEFLOW_PORT=8000

RUN mkdir -p /data/workflows /data/environments /data/history

EXPOSE 8000

CMD ["python", "app.py"]
