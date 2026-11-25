# Local (arm64) variant of app/Dockerfile: identical except a compiler is installed because wordllama ships no linux-aarch64 wheel.
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends g++ >/dev/null && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" asyncpg redis numpy wordllama
RUN python -c "import wordllama, os, shutil; b=os.path.dirname(wordllama.__file__); os.makedirs(b+'/tokenizer', exist_ok=True); shutil.copy(b+'/tokenizers/l2_supercat_tokenizer_config.json', b+'/tokenizer/')"
COPY main.py queries.py invalidator.py /app/
EXPOSE 8000
