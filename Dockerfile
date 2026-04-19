FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV STATE_PATH=/data/state.json
VOLUME ["/data"]

ENTRYPOINT ["python", "main.py"]
CMD ["poll"]
