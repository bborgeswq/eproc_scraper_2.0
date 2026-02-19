FROM python:3.12-slim

# Instalar dependências do sistema para Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium do Playwright
RUN playwright install chromium

# Copiar código
COPY . .

# Criar diretório temporário para downloads
RUN mkdir -p /app/tmp_docs

# Variáveis de ambiente padrão para produção
ENV HEADLESS=true
ENV TEMP_DIR=/app/tmp_docs
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.main"]
