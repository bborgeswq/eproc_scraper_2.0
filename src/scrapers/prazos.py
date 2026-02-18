import re
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import Page
from src.config import Config

BR_TZ = ZoneInfo("America/Sao_Paulo")


def _parse_datetime_br(text: str) -> datetime | None:
    """Converte data brasileira '06/02/2026 09:09:00' para datetime UTC."""
    text = text.strip()
    if not text:
        return None
    try:
        dt = datetime.strptime(text, "%d/%m/%Y %H:%M:%S")
        return dt.replace(tzinfo=BR_TZ)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%d/%m/%Y")
            return dt.replace(tzinfo=BR_TZ)
        except ValueError:
            return None


def _extract_cnj(text: str) -> str | None:
    """Extrai número CNJ do texto (formato NNNNNNN-NN.NNNN.N.NN.NNNN)."""
    match = re.search(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", text)
    return match.group(0) if match else None


async def scrape_prazos_abertos(page: Page) -> dict[str, dict]:
    """
    Navega para a tabela de prazos abertos e extrai todos os registros.
    Retorna dict {cnj: dados_do_prazo}.
    """
    # Navegar para prazos abertos
    print("[PRAZOS] Navegando para prazos abertos...")
    link = page.locator("a[href*='citacao_intimacao_prazo_aberto_listar']").first
    href = await link.get_attribute("href")
    await page.goto(f"{Config.EPROC_BASE_URL}/eproc/{href}", wait_until="networkidle")

    title = await page.title()
    print(f"[PRAZOS] Pagina carregada: {title}")

    # Encontrar a tabela principal (infraTable com mais linhas)
    tables = page.locator("table.infraTable")
    table_count = await tables.count()

    main_table = None
    max_rows = 0
    for i in range(table_count):
        t = tables.nth(i)
        rows = await t.locator("tr").count()
        if rows > max_rows:
            max_rows = rows
            main_table = t

    if not main_table:
        print("[PRAZOS] ERRO: Tabela principal nao encontrada")
        return {}

    print(f"[PRAZOS] Tabela encontrada com {max_rows} linhas")

    # Extrair linhas de dados
    # Cada processo ocupa 1 linha na tabela com colunas:
    # [checkbox, Processo, Classe, Assunto, Evento e Prazo, Data envio, Inicio Prazo, Final Prazo]
    rows = main_table.locator("tr")
    row_count = await rows.count()

    processos = {}

    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = await cells.count()

        # Pular linhas de header ou com menos de 5 colunas
        if cell_count < 5:
            continue

        try:
            # Coluna do processo (contém CNJ, juízo, partes)
            proc_cell = cells.nth(1)
            proc_text = (await proc_cell.text_content() or "").strip()
            cnj = _extract_cnj(proc_text)

            if not cnj:
                continue

            # Extrair juízo do texto do processo
            juizo = ""
            juizo_match = re.search(r"Ju[ií]zo:\s*(.+?)(?:\n|Cadastrar)", proc_text)
            if juizo_match:
                juizo = juizo_match.group(1).strip()

            # Extrair partes
            partes_raw = proc_text

            # Classe
            classe = (await cells.nth(2).text_content() or "").strip()

            # Assunto
            assunto = (await cells.nth(3).text_content() or "").strip()

            # Evento e Prazo
            evento_prazo = (await cells.nth(4).text_content() or "").strip()

            # Data envio requisição
            data_envio_text = (await cells.nth(5).text_content() or "").strip()
            data_envio = _parse_datetime_br(data_envio_text)

            # Início Prazo
            inicio_text = (await cells.nth(6).text_content() or "").strip()
            prazo_inicio = _parse_datetime_br(inicio_text)

            # Final Prazo
            final_text = (await cells.nth(7).text_content() or "").strip()
            prazo_final = _parse_datetime_br(final_text)

            # Extrair link do processo para navegação posterior
            proc_link = proc_cell.locator("a[href*='processo_selecionar']")
            proc_href = ""
            if await proc_link.count() > 0:
                proc_href = await proc_link.first.get_attribute("href") or ""

            processos[cnj] = {
                "cnj": cnj,
                "classe": classe,
                "assunto": assunto,
                "juizo": juizo,
                "evento_descricao": evento_prazo,
                "data_envio": data_envio.isoformat() if data_envio else None,
                "prazo_inicio": prazo_inicio.isoformat() if prazo_inicio else None,
                "prazo_final": prazo_final.isoformat() if prazo_final else None,
                "proc_href": proc_href,
                "partes_raw": partes_raw,
            }

        except Exception as e:
            print(f"[PRAZOS] Erro ao processar linha {i}: {e}")
            continue

    print(f"[PRAZOS] {len(processos)} processos extraidos")
    return processos
