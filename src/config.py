import os
import sys
from dotenv import load_dotenv

load_dotenv()


class Config:
    EPROC_USERNAME = os.getenv("EPROC_USERNAME", "")
    EPROC_PASSWORD = os.getenv("EPROC_PASSWORD", "")
    TOTP_SECRET = os.getenv("TOTP_SECRET", "")
    ADV_NAME = os.getenv("ADV_NAME", "")
    HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
    PROCESS_LIMIT = int(os.getenv("PROCESS_LIMIT", "0"))

    EPROC_BASE_URL = "https://eproc1g.tjrs.jus.br"
    EPROC_LOGIN_URL = f"{EPROC_BASE_URL}/eproc/externo_controlador.php?acao=SSO%2Flogin"

    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
    TEMP_DIR = os.getenv("TEMP_DIR", "./tmp_docs")
    STORAGE_BUCKET = "process-documents"

    # Proxy (opcional)
    PROXY_SERVER = os.getenv("PROXY_SERVER", "")
    PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
    PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")

    @classmethod
    def validate(cls):
        missing = []
        if not cls.EPROC_USERNAME:
            missing.append("EPROC_USERNAME")
        if not cls.EPROC_PASSWORD:
            missing.append("EPROC_PASSWORD")
        if not cls.TOTP_SECRET:
            missing.append("TOTP_SECRET")
        if not cls.SUPABASE_URL:
            missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY:
            missing.append("SUPABASE_KEY")
        if missing:
            print(f"[ERRO] Variáveis de ambiente faltando: {', '.join(missing)}")
            print("Configure o arquivo .env com base no .env.example")
            sys.exit(1)

        os.makedirs(cls.TEMP_DIR, exist_ok=True)
