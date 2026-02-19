import re
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import Page, BrowserContext
from src.config import Config

BR_TZ = ZoneInfo("America/Sao_Paulo")


def _parse_datetime_br(text: str) -> datetime | None:
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


async def open_process_page(context: BrowserContext, page: Page, proc_href: str) -> Page:
    """Abre a pagina do processo em nova aba e retorna a Page."""
    full_url = f"{Config.EPROC_BASE_URL}/eproc/{proc_href}"
    proc_page = await context.new_page()
    await proc_page.goto(full_url, wait_until="networkidle")
    return proc_page


async def extract_header(page: Page) -> dict:
    """Extrai dados do cabecalho do processo."""
    cnj = ""
    el = page.locator("#txtNumProcesso")
    if await el.count() > 0:
        cnj = (await el.text_content() or "").strip()

    classe = ""
    el = page.locator("#txtClasse")
    if await el.count() > 0:
        classe = (await el.text_content() or "").strip()

    competencia = ""
    el = page.locator("#txtCompetencia")
    if await el.count() > 0:
        competencia = (await el.text_content() or "").strip()

    # Data de autuacao
    data_autuacao = None
    body_text = await page.locator("#divCapaProcesso").text_content() or ""
    match = re.search(r"Data de autua[çc][aã]o:\s*(\d{2}/\d{2}/\d{4})", body_text)
    if match:
        # Converter de DD/MM/YYYY para YYYY-MM-DD (ISO)
        try:
            dt = datetime.strptime(match.group(1), "%d/%m/%Y")
            data_autuacao = dt.strftime("%Y-%m-%d")
        except ValueError:
            data_autuacao = None

    # Situacao
    situacao = ""
    match = re.search(r"Situa[çc][aã]o\s*(.+?)(?:\n|Ó)", body_text)
    if match:
        situacao = match.group(1).strip()

    # Orgao julgador
    orgao_julgador = ""
    match = re.search(r"[OÓ]rg[aã]o Julgador:\s*\n?\s*(.+?)(?:\n|Juiz)", body_text)
    if match:
        orgao_julgador = match.group(1).strip()

    # Juiz
    juiz = ""
    match = re.search(r"Juiz\(a\):\s*\n?\s*(.+?)(?:\n|Processos)", body_text)
    if match:
        juiz = match.group(1).strip()

    # Processos relacionados
    relacionados = []
    rel_table = page.locator("#tableRelacionado")
    if await rel_table.count() > 0:
        rel_text = await rel_table.text_content() or ""
        relacionados = re.findall(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", rel_text)

    return {
        "cnj": cnj,
        "classe": classe,
        "competencia": competencia,
        "data_autuacao": data_autuacao,
        "situacao": situacao,
        "orgao_julgador": orgao_julgador,
        "juiz": juiz,
        "processos_relacionados": relacionados,
    }


async def extract_assuntos(page: Page) -> list[dict]:
    """Extrai assuntos da tabela de assuntos."""
    assuntos = []
    table = page.locator("table.infraTable.table-not-hover.mb-0")
    if await table.count() == 0:
        return assuntos

    rows = table.locator("tr")
    row_count = await rows.count()
    for i in range(row_count):
        cells = rows.nth(i).locator("td")
        if await cells.count() >= 2:
            codigo = (await cells.nth(0).text_content() or "").strip()
            descricao = (await cells.nth(1).text_content() or "").strip()
            if codigo and descricao:
                assuntos.append({"codigo": codigo, "descricao": descricao})

    return assuntos


async def extract_partes(page: Page) -> list[dict]:
    """Extrai partes e representantes via DOM da tabela de partes."""
    partes = []
    table = page.locator("#tblPartesERepresentantes")
    if await table.count() == 0:
        return partes

    # Clicar em "e outros" para carregar todas as partes (se existir)
    outros_links = table.locator("a:has-text('e outros')")
    outros_count = await outros_links.count()
    for i in range(outros_count):
        try:
            await outros_links.nth(i).click()
            await page.wait_for_timeout(1000)
        except Exception:
            pass

    # Seletor amplo: a.infraNomeParte OU a[data-parte] para pegar todos os tipos
    # (REQUERENTE, REQUERIDO, EXEQUENTE, EXECUTADO, HERDEIRO,
    #  REPRESENTANTE LEGAL, MINISTÉRIO PÚBLICO, etc.)
    nome_links = table.locator("a.infraNomeParte, a[data-parte]")
    count = await nome_links.count()

    # Regex para procuradores/advogados: NOME   REGISTRO
    # Suporta: RS053253, OAB12345, DPE-4594967, SC099999, etc.
    _ESTADOS = (
        "RS|SC|PR|SP|RJ|MG|BA|PE|CE|GO|MT|MS|PA|AM|MA|PI|RN|PB|SE|AL|ES|"
        "RO|AC|AP|RR|TO|DF|OAB"
    )
    rep_regex = re.compile(
        rf"([A-ZÀ-Ú][A-ZÀ-Ú\s\.]+?)"         # nome (somente MAIÚSCULAS, sem IGNORECASE)
        rf"\s{{2,}}"                             # 2+ espaços separadores
        rf"((?:{_ESTADOS}|DPE)[-]?\d+)",         # registro (OAB ou DPE, case-insensitive no estado)
        re.UNICODE
    )

    for i in range(count):
        link = nome_links.nth(i)
        nome = (await link.text_content() or "").strip()
        tipo = (await link.get_attribute("data-parte") or "").strip().upper()

        if not nome:
            continue

        # Mapear tipos curtos/variantes
        tipo_map = {"REU": "RÉU", "A": "AUTOR", "R": "RÉU"}
        tipo = tipo_map.get(tipo, tipo)

        # Buscar CPF/CNPJ no elemento pai (td ou container mais próximo)
        td = link.locator("xpath=ancestor::td[1]")
        if await td.count() == 0:
            td = link.locator("xpath=ancestor::div[1]")
        cpf_cnpj = ""

        if await td.count() > 0:
            cpf_span = td.locator("span[id^='spnCpfParte']")
            if await cpf_span.count() > 0:
                for j in range(await cpf_span.count()):
                    cpf_text = (await cpf_span.nth(j).text_content() or "").strip()
                    if cpf_text:
                        cpf_cnpj = cpf_text
                        break

            # Fallback: extrair CPF/CNPJ do texto se span não encontrado
            if not cpf_cnpj:
                td_text = await td.text_content() or ""
                cpf_match = re.search(r"\((\d{3}\.\d{3}\.\d{3}-\d{2})\)", td_text)
                if not cpf_match:
                    cpf_match = re.search(r"\((\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\)", td_text)
                if cpf_match:
                    cpf_cnpj = cpf_match.group(1)

        # Extrair qualificação (Inventariante, Espólio, etc.)
        qualificacao = ""
        if await td.count() > 0:
            td_text_full = await td.text_content() or ""
            qual_match = re.search(r"\((\w+(?:\s+\w+)?)\)\s*-\s*Pessoa", td_text_full)
            if qual_match and qual_match.group(1) not in [cpf_cnpj]:
                qualificacao = qual_match.group(1).strip()

        # Extrair advogados/procuradores do mesmo td
        representantes = []
        if await td.count() > 0:
            td_text = await td.text_content() or ""

            for rep_match in rep_regex.finditer(td_text):
                rep_nome = rep_match.group(1).strip()
                rep_registro = rep_match.group(2).strip()

                # Limpar prefixos espúrios capturados pelo regex
                rep_nome = re.sub(
                    r"^(?:Procurador\(es\):\s*|ADVOGADO\s*|ADVOGADA\s*|"
                    r"Pessoa\s+F[ií]sica\s*|Pessoa\s+Jur[ií]dica\s*)",
                    "", rep_nome, flags=re.IGNORECASE
                ).strip()

                # Não incluir o nome da própria parte como representante
                if rep_nome and rep_nome.upper() != nome.upper():
                    tipo_rep = "DPE" if "DPE" in rep_registro.upper() else "Advogado"
                    representantes.append({
                        "nome": rep_nome,
                        "oab": rep_registro,
                        "tipo": tipo_rep,
                    })

        partes.append({
            "tipo": tipo,
            "nome": nome,
            "cpf_cnpj": cpf_cnpj,
            "qualificacao": qualificacao,
            "representantes": representantes,
        })

    return partes


def identify_adv_side(partes: list[dict], adv_name: str) -> str:
    """Identifica de qual lado o advogado atua.
    Faz match por nome (case-insensitive, parcial) e por OAB/registro.
    Retorna o tipo da parte principal (AUTOR/RÉU/REQUERENTE/EXEQUENTE/etc)."""
    if not adv_name:
        return ""
    adv_upper = adv_name.upper()
    for parte in partes:
        # Pular partes que são "extras" (HERDEIRO, MINISTÉRIO PÚBLICO, etc.)
        # mas que herdam o mesmo advogado - procurar nas partes principais primeiro
        for rep in parte.get("representantes", []):
            rep_nome = (rep.get("nome") or "").upper()
            rep_oab = (rep.get("oab") or "").upper()
            # Match por OAB (ex: ADV_NAME="RS053253" ou "JAIME DARLAN MARTINS")
            if adv_upper == rep_oab:
                return parte.get("tipo", "")
            if adv_upper in rep_nome or rep_nome in adv_upper:
                return parte.get("tipo", "")
    return ""


def _is_yellow(bg_color: str) -> bool:
    """Verifica se uma cor de fundo é amarela."""
    if not bg_color:
        return False
    bg = bg_color.lower()
    return (
        "yellow" in bg
        or "rgb(255, 255, 0" in bg
        or "rgb(255, 255, 1" in bg
        or "rgb(255, 255, 2" in bg
    )


async def extract_eventos(page: Page) -> list[dict]:
    """Extrai todos os eventos da tabela de eventos."""
    eventos = []
    table = page.locator("#tblEventos")
    if await table.count() == 0:
        return eventos

    # Clicar em "Carregar TODOS os eventos" se existir (paginação)
    load_all = page.locator("a:has-text('Carregar TODOS os eventos')")
    if await load_all.count() > 0:
        await load_all.click()
        # Aguardar carregamento dos eventos adicionais
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    rows = table.locator("tr")
    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = await cells.count()

        if cell_count < 4:
            continue

        try:
            # Coluna 0: Número do evento
            num_text = (await cells.nth(0).text_content() or "").strip()
            num_match = re.search(r"(\d+)", num_text)
            if not num_match:
                continue
            numero = int(num_match.group(1))

            # Coluna 1: Data/Hora
            data_text = (await cells.nth(1).text_content() or "").strip()
            data_hora = _parse_datetime_br(data_text)
            if not data_hora:
                continue

            # Coluna 2: Descrição + detecção de prazo aberto (fundo amarelo)
            desc_cell = cells.nth(2)
            descricao = (await desc_cell.text_content() or "").strip()

            # Detectar prazo aberto: APENAS a célula de descrição é amarela
            # (linha inteira amarela = outro significado, não é prazo aberto)
            desc_bg = await desc_cell.evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
            row_bg = await cells.nth(0).evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
            desc_is_yellow = _is_yellow(desc_bg)
            row_is_yellow = _is_yellow(row_bg)
            prazo_aberto_visual = desc_is_yellow and not row_is_yellow

            # Coluna 3: Usuário
            usuario = (await cells.nth(3).text_content() or "").strip()

            # Coluna 4: Documentos (links)
            docs = []
            if cell_count > 4:
                doc_links = cells.nth(4).locator("a[href*='acessar_documento']")
                doc_count = await doc_links.count()
                for d in range(doc_count):
                    doc_link = doc_links.nth(d)
                    doc_nome = (await doc_link.text_content() or "").strip()
                    doc_href = await doc_link.get_attribute("href") or ""
                    if doc_nome and doc_href:
                        docs.append({
                            "nome": doc_nome,
                            "url_eproc": doc_href,
                        })

            # Detectar se é evento de prazo (por texto, complementar à cor)
            tem_prazo_texto = "Prazo:" in descricao and "Status:" in descricao
            prazo_dias = None
            prazo_status = None
            prazo_data_inicial = None
            prazo_data_final = None
            evento_referencia = None
            urgente = "URGENTE" in descricao

            if tem_prazo_texto:
                # Prazo: 5 dias
                dias_match = re.search(r"Prazo:\s*(\d+)\s*dias?", descricao)
                if dias_match:
                    prazo_dias = int(dias_match.group(1))

                # Status:ABERTO ou Status:FECHADO (34 - RÉPLICA)
                status_match = re.search(r"Status:\s*(\w+)", descricao)
                if status_match:
                    prazo_status = status_match.group(1)

                # Data inicial da contagem do prazo: 11/02/2026 00:00:00
                inicio_match = re.search(
                    r"Data inicial[^:]*:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
                    descricao
                )
                if inicio_match:
                    prazo_data_inicial = _parse_datetime_br(inicio_match.group(1))

                # Data final: 19/02/2026 23:59:59
                final_match = re.search(
                    r"Data final:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
                    descricao
                )
                if final_match:
                    prazo_data_final = _parse_datetime_br(final_match.group(1))

            # Refer. ao Evento NNN
            ref_match = re.search(r"Refer\.\s*ao\s*Evento:?\s*(\d+)", descricao)
            if ref_match:
                evento_referencia = int(ref_match.group(1))

            eventos.append({
                "numero": numero,
                "data_hora": data_hora.isoformat(),
                "descricao": descricao,
                "usuario": usuario,
                "prazo_aberto": prazo_aberto_visual,
                "prazo_dias": prazo_dias,
                "prazo_status": prazo_status,
                "prazo_data_inicial": prazo_data_inicial.isoformat() if prazo_data_inicial else None,
                "prazo_data_final": prazo_data_final.isoformat() if prazo_data_final else None,
                "evento_referencia": evento_referencia,
                "urgente": urgente,
                "documentos": docs,
            })

        except Exception as e:
            print(f"[PROCESSO] Erro ao processar evento na linha {i}: {e}")
            continue

    print(f"[PROCESSO] {len(eventos)} eventos extraidos")
    return eventos
