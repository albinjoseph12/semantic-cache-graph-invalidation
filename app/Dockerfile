FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" asyncpg redis numpy wordllama
# WordLlama looks for its tokenizer under <pkg>/tokenizer/ but ships it under <pkg>/tokenizers/ (offline fix)
RUN python -c "import wordllama, os, shutil; b=os.path.dirname(wordllama.__file__); os.makedirs(b+'/tokenizer', exist_ok=True); shutil.copy(b+'/tokenizers/l2_supercat_tokenizer_config.json', b+'/tokenizer/')"
COPY main.py queries.py invalidator.py /app/
EXPOSE 8000
