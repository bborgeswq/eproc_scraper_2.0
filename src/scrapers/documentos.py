import os
import hashlib
from uuid import uuid4
from playwright.async_api import BrowserContext, Download
from src.config import Config

# Timeouts generosos para proxy lento com documentos grandes
_GOTO_TIMEOUT = 120_000       # 2 min para navegar
_DOWNLOAD_TIMEOUT = 300_000   # 5 min para download iniciar/completar
_REQUEST_TIMEOUT = 300_000    # 5 min para request.get direto

# Mapeamento de magic bytes → (extensão, tipo)
_MAGIC_MAP = [
    (b"%PDF",                       ".pdf",  "PDF"),
    (b"\x89PNG\r\n\x1a\n",         ".png",  "IMG"),
    (b"\xff\xd8\xff",              ".jpg",  "IMG"),
    (b"GIF87a",                    ".gif",  "IMG"),
    (b"GIF89a",                    ".gif",  "IMG"),
    (b"RIFF",                      ".webp", "IMG"),   # RIFF....WEBP (verificado abaixo)
    (b"\x00\x00\x00\x1cftyp",     ".mp4",  "VIDEO"),
    (b"\x00\x00\x00\x18ftyp",     ".mp4",  "VIDEO"),
    (b"\x00\x00\x00\x20ftyp",     ".mp4",  "VIDEO"),
    (b"\x1aE\xdf\xa3",            ".webm", "VIDEO"),
    (b"ID3",                       ".mp3",  "AUDIO"),
    (b"\xff\xfb",                  ".mp3",  "AUDIO"),
    (b"\xff\xf3",                  ".mp3",  "AUDIO"),
    (b"OggS",                      ".ogg",  "AUDIO"),
    (b"fLaC",                      ".flac", "AUDIO"),
    (b"PK\x03\x04",               ".zip",  "ARQUIVO"),
]


def _detect_format(data: bytes) -> tuple[str, str, str]:
    """Detecta formato pelos magic bytes. Retorna (extensão, tipo, label)."""
    for magic, ext, tipo in _MAGIC_MAP:
        if data[:len(magic)] == magic:
            # RIFF pode ser WAV ou WEBP
            if magic == b"RIFF" and len(data) >= 12:
                if data[8:12] == b"WEBP":
                    return ".webp", "IMG", "img/webp"
                elif data[8:12] == b"WAVE":
                    return ".wav", "AUDIO", "audio/wav"
            return ext, tipo, f"{tipo.lower()}/{ext.lstrip('.')}"
    # Fallback: se começa com < provavelmente é HTML
    if data[:1] == b"<" or data[:5] == b"<!DOC":
        return ".html", "HTML", "html"
    return ".bin", "OUTRO", "desconhecido"


def _sha256_file(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_result(temp_path: str, tipo: str = "PDF") -> dict:
    return {
        "local_path": temp_path,
        "tipo": tipo,
        "tamanho_bytes": os.path.getsize(temp_path),
        "hash_sha256": _sha256_file(temp_path),
    }


async def download_document(context: BrowserContext, url_eproc: str) -> dict | None:
    """
    Faz download de um documento do eProc.
    Retorna {local_path, tipo, tamanho_bytes, hash_sha256} ou None se falhar.

    Suporta PDF, imagens, vídeo, áudio e outros formatos.

    Estratégia (em ordem):
    1. Download direto (arquivo que baixa automaticamente ao navegar)
    2. Botão de download no PDF viewer do eProc (canto superior direito)
    3. Extrair URL do conteúdo embedded (embed/iframe/object src)
    4. Link direto para download na página
    5. Documento HTML do sistema → renderizar para PDF
    """
    full_url = f"{Config.EPROC_BASE_URL}/eproc/{url_eproc}"
    doc_page = await context.new_page()
    temp_id = str(uuid4())
    temp_path = os.path.join(Config.TEMP_DIR, f"{temp_id}.bin")  # extensão definitiva depois

    try:
        # === Tentativa 1: Download direto (arquivo auto-download) ===
        try:
            async with doc_page.expect_download(timeout=60_000) as download_info:
                await doc_page.goto(full_url, timeout=_GOTO_TIMEOUT)
            download: Download = await download_info.value
            await download.save_as(temp_path)
            temp_path, tipo = _detect_and_rename(temp_path, temp_id)
            print(f"    [download direto] {tipo}")
            await doc_page.close()
            return _build_result(temp_path, tipo)
        except Exception:
            pass  # Não é download direto, página carregou normalmente

        # Aguardar carregamento completo
        try:
            await doc_page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

        # === Tentativa 2: Clicar no botão de download do PDF viewer ===
        download_selectors = [
            "button#download",
            "a#download",
            "#downloadButton",
            "button[title*='ownload']",
            "a[title*='ownload']",
            "button[title*='Baixar']",
            "a[title*='Baixar']",
            "[data-action='download']",
            "#secondaryDownload",
            "#download",
            "button.download",
            "a.download",
        ]
        selector_combined = ", ".join(download_selectors)
        download_btn = doc_page.locator(selector_combined)

        if await download_btn.count() > 0:
            try:
                async with doc_page.expect_download(timeout=_DOWNLOAD_TIMEOUT) as dl_info:
                    await download_btn.first.click()
                download: Download = await dl_info.value
                await download.save_as(temp_path)
                temp_path, tipo = _detect_and_rename(temp_path, temp_id)
                print(f"    [botao download] {tipo}")
                await doc_page.close()
                return _build_result(temp_path, tipo)
            except Exception as e:
                print(f"    [botao download falhou: {e}]")

        # === Tentativa 3: Extrair URL do conteúdo embedded ===
        embed_locator = doc_page.locator(
            "embed[type='application/pdf'], "
            "embed[src*='.pdf'], "
            "embed[src], "
            "iframe[src*='pdf'], "
            "iframe[src*='documento'], "
            "iframe[src*='acessar'], "
            "object[type='application/pdf'], "
            "object[data*='.pdf'], "
            "object[data]"
        )
        if await embed_locator.count() > 0:
            embed_src = (
                await embed_locator.first.get_attribute("src")
                or await embed_locator.first.get_attribute("data")
            )
            if embed_src:
                if not embed_src.startswith("http"):
                    embed_src = f"{Config.EPROC_BASE_URL}/eproc/{embed_src}"
                response = await doc_page.context.request.get(
                    embed_src, timeout=_REQUEST_TIMEOUT
                )
                body = await response.body()
                if len(body) > 0:
                    ext, tipo, label = _detect_format(body)
                    # Aceitar qualquer formato válido (não apenas PDF)
                    if tipo != "HTML":
                        final_path = os.path.join(Config.TEMP_DIR, f"{temp_id}{ext}")
                        with open(final_path, "wb") as f:
                            f.write(body)
                        print(f"    [embed src] {label}")
                        await doc_page.close()
                        return _build_result(final_path, tipo)

        # === Tentativa 4: Buscar link direto para download na página ===
        doc_links = doc_page.locator(
            "a[href*='download'], "
            "a[href*='.pdf'], "
            "a[href*='acessar_documento_implementacao']"
        )
        if await doc_links.count() > 0:
            try:
                async with doc_page.expect_download(timeout=_DOWNLOAD_TIMEOUT) as dl_info:
                    await doc_links.first.click()
                download: Download = await dl_info.value
                await download.save_as(temp_path)
                temp_path, tipo = _detect_and_rename(temp_path, temp_id)
                print(f"    [link download] {tipo}")
                await doc_page.close()
                return _build_result(temp_path, tipo)
            except Exception:
                pass

        # === Tentativa 5: Documento HTML do sistema (certidões, mandados, despachos) ===
        content_area = doc_page.locator("#divInfraAreaTelaD, #divDocumento, .infraAreaTelaD, body")
        if await content_area.first.count() > 0:
            await doc_page.evaluate("""
                () => {
                    const hide = ['#divInfraBarraNavegacao', '#divInfraBarraSistema',
                                  '#divInfraBarraComandosSuperior', '#divInfraBarraLocalizacao',
                                  '.infraBarraComandos', '#divInfraAreaMenu', 'header', 'nav',
                                  '#fldAnexos', '#divInfraBarraComandosInferior'];
                    hide.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.style.display = 'none');
                    });
                }
            """)
            pdf_path = os.path.join(Config.TEMP_DIR, f"{temp_id}.pdf")
            await doc_page.pdf(
                path=pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
            )
            print(f"    [html->pdf]")
            await doc_page.close()
            # Limpar .bin temporário se existir
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return _build_result(pdf_path, tipo="HTML")

        print(f"    [FALHA] Nenhum método de download funcionou")
        print(f"    URL: {full_url}")
        print(f"    Título: {await doc_page.title()}")
        await doc_page.close()
        return None

    except Exception as e:
        print(f"[DOC] Erro ao baixar documento: {e}")
        try:
            await doc_page.close()
        except Exception:
            pass
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return None


def _detect_and_rename(filepath: str, temp_id: str) -> tuple[str, str]:
    """Detecta formato do arquivo e renomeia com extensão correta.
    Retorna (novo_path, tipo)."""
    with open(filepath, "rb") as f:
        header = f.read(32)
    ext, tipo, _ = _detect_format(header)
    new_path = os.path.join(Config.TEMP_DIR, f"{temp_id}{ext}")
    if new_path != filepath:
        os.rename(filepath, new_path)
    return new_path, tipo
