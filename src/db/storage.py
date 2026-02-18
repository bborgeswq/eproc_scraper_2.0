import os
import unicodedata
from src.config import Config
from src.db.client import get_supabase

# Mapeamento extensão → content-type para upload
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".zip": "application/zip",
    ".html": "text/html",
}


def upload_document(local_path: str, storage_path: str) -> str:
    """
    Upload de arquivo para Supabase Storage.
    Retorna a URL publica do documento.
    Deleta o arquivo local apos upload.
    """
    sb = get_supabase()

    ext = os.path.splitext(local_path)[1].lower()
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    with open(local_path, "rb") as f:
        sb.storage.from_(Config.STORAGE_BUCKET).upload(
            path=storage_path,
            file=f,
            file_options={"content-type": content_type, "upsert": "true"},
        )

    url = sb.storage.from_(Config.STORAGE_BUCKET).get_public_url(storage_path)

    # Deletar arquivo local
    os.remove(local_path)

    return url


def delete_process_documents(cnj: str):
    """Remove todos os documentos de um processo do Storage (recursivo)."""
    sb = get_supabase()
    try:
        # Estrutura: {cnj}/evt_XX/arquivo.pdf — precisamos listar subpastas primeiro
        folders = sb.storage.from_(Config.STORAGE_BUCKET).list(path=cnj)
        for folder in (folders or []):
            folder_path = f"{cnj}/{folder['name']}"
            # Listar arquivos dentro de cada subpasta (evt_01, evt_02, ...)
            files = sb.storage.from_(Config.STORAGE_BUCKET).list(path=folder_path)
            if files:
                paths = [f"{folder_path}/{f['name']}" for f in files]
                sb.storage.from_(Config.STORAGE_BUCKET).remove(paths)
    except Exception as e:
        print(f"[STORAGE] Erro ao deletar docs de {cnj}: {e}")


def build_storage_path(cnj: str, numero_evento: int, nome_doc: str, ext: str = ".pdf") -> str:
    """Constroi o path de storage: {cnj}/evt_{num}/{nome}.{ext}"""
    # Remover acentos (ex: OFÍCIO → OFICIO, INTIMAÇÃO → INTIMACAO)
    safe_name = unicodedata.normalize("NFKD", nome_doc)
    safe_name = safe_name.encode("ascii", "ignore").decode("ascii")
    safe_name = safe_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
    # Remover extensão existente e aplicar a correta
    for known_ext in (".pdf", ".png", ".jpg", ".mp4", ".mp3", ".zip", ".html", ".bin"):
        if safe_name.lower().endswith(known_ext):
            safe_name = safe_name[:-len(known_ext)]
            break
    safe_name += ext
    return f"{cnj}/evt_{numero_evento:02d}/{safe_name}"
