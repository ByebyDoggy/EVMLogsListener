FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY src/ ./src/
COPY config.yaml.example /app/config.yaml

ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["python", "-m", "evm_chain_listener.main", "-c", "/app/config.yaml"]
