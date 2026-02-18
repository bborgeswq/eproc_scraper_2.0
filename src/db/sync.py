import os
import asyncio
from datetime import datetime, timezone
from playwright.async_api import Page, BrowserContext
from src.db.client import get_supabase
from src.db.storage import upload_document, delete_process_documents, build_storage_path
from src.scrapers.prazos import scrape_prazos_abertos
from src.scrapers.processo import open_process_page, extract_header, extract_assuntos, extract_partes, extract_eventos, identify_adv_side
from src.scrapers.documentos import download_document
from src.config import Config


async def sync(page: Page, context: BrowserContext):
    """Algoritmo principal de sync entre eProc e Supabase."""
    sb = get_supabase()
    log_id = _create_sync_log(sb)
    stats = {"added": 0, "removed": 0, "updated": 0, "docs_uploaded": 0}

    try:
        # 1. Scrapear prazos abertos do eProc (dict ordenado pela tabela)
        eproc = await scrape_prazos_abertos(page)
        eproc_cnjs_ordered = list(eproc.keys())  # Mantém ordem da tabela
        eproc_cnjs_set = set(eproc_cnjs_ordered)

        # 2. Carregar CNJs da DB
        result = sb.table("processos").select("cnj").execute()
        db_cnjs = {row["cnj"] for row in result.data}

        # 3. Calcular diff (listas ordenadas pela tabela do eProc)
        to_add = [cnj for cnj in eproc_cnjs_ordered if cnj not in db_cnjs]
        to_remove = [cnj for cnj in db_cnjs if cnj not in eproc_cnjs_set]
        to_keep = [cnj for cnj in eproc_cnjs_ordered if cnj in db_cnjs]

        print(f"\n[SYNC] Diff: +{len(to_add)} novos | -{len(to_remove)} removidos | {len(to_keep)} mantidos")

        if Config.PROCESS_LIMIT > 0:
            print(f"[SYNC] *** MODO TESTE: limite de {Config.PROCESS_LIMIT} processo(s) ***")

        # 4. Remover processos que sairam do eProc
        # Proteção: se eProc retornou 0 processos mas DB tem dados, é provável
        # erro de navegação — não deletar nada
        if len(eproc_cnjs_set) == 0 and len(db_cnjs) > 0:
            print(f"[SYNC] AVISO: eProc retornou 0 processos mas DB tem {len(db_cnjs)}. Pulando remoção (possível erro de navegação).")
            to_remove = []

        for cnj in to_remove:
            print(f"[SYNC] Removendo: {cnj}")
            delete_process_documents(cnj)
            sb.table("processos").delete().eq("cnj", cnj).execute()
            stats["removed"] += 1

        # 5. Adicionar processos novos (scrape completo)
        added_count = 0
        for cnj in to_add:
            if Config.PROCESS_LIMIT > 0 and added_count >= Config.PROCESS_LIMIT:
                print(f"[SYNC] Limite de teste atingido ({Config.PROCESS_LIMIT}), pulando restante")
                break
            print(f"\n[SYNC] Adicionando: {cnj}")
            try:
                await _add_full_process(context, page, sb, cnj, eproc[cnj], stats)
                stats["added"] += 1
            except Exception as e:
                print(f"[SYNC] ERRO ao adicionar {cnj}: {e}")
                stats["errors"] = stats.get("errors", 0) + 1
            added_count += 1
            await asyncio.sleep(1)

        # 6. Atualizar processos existentes (campos de prazo + eventos novos)
        updated_count = 0
        for cnj in to_keep:
            if Config.PROCESS_LIMIT > 0 and updated_count >= Config.PROCESS_LIMIT:
                print(f"[SYNC] Limite de teste atingido ({Config.PROCESS_LIMIT}), pulando restante")
                break
            print(f"[SYNC] Atualizando: {cnj}")
            try:
                await _update_process(context, page, sb, cnj, eproc[cnj], stats)
                stats["updated"] += 1
            except Exception as e:
                print(f"[SYNC] ERRO ao atualizar {cnj}: {e}")
                stats["errors"] = stats.get("errors", 0) + 1
            updated_count += 1
            await asyncio.sleep(1)

        # Verificar se há processos pendentes (não processados por causa do LIMIT)
        total_to_process = len(to_add) + len(to_keep)
        total_processed = added_count + updated_count
        has_pending = Config.PROCESS_LIMIT > 0 and total_processed < total_to_process

        errors = stats.get("errors", 0)
        status = "success" if errors == 0 else "partial"
        _finish_sync_log(sb, log_id, status, stats)
        print(f"\n[SYNC] Concluido! +{stats['added']} -{stats['removed']} ~{stats['updated']} docs:{stats['docs_uploaded']} erros:{errors}")
        if has_pending:
            print(f"[SYNC] Pendentes: {total_to_process - total_processed} processos ainda não processados")

        stats["has_pending"] = has_pending
        stats["errors"] = errors
        return stats

    except Exception as e:
        _finish_sync_log(sb, log_id, "error", stats, str(e))
        print(f"[SYNC] ERRO: {e}")
        raise


async def _add_full_process(context, page, sb, cnj, prazo_data, stats):
    """Scrape completo de um processo e insere tudo na DB."""
    proc_page = await open_process_page(context, page, prazo_data["proc_href"])

    try:
        # Extrair header
        header = await extract_header(proc_page)

        # Extrair assuntos e partes (agora JSONB direto no processos)
        assuntos = await extract_assuntos(proc_page)
        partes = await extract_partes(proc_page)

        # Identificar lado do advogado
        lado = identify_adv_side(partes, Config.ADV_NAME)

        # Inserir processo com assuntos e partes como JSONB
        result = sb.table("processos").upsert({
            "cnj": cnj,
            "classe": header.get("classe") or prazo_data.get("classe"),
            "competencia": header.get("competencia"),
            "data_autuacao": header.get("data_autuacao"),
            "situacao": header.get("situacao"),
            "orgao_julgador": header.get("orgao_julgador"),
            "juiz": header.get("juiz"),
            "juizo": prazo_data.get("juizo"),
            "lado_advogado": lado,
            "processos_relacionados": header.get("processos_relacionados", []),
            "assuntos": assuntos,
            "partes": partes,
            "prazo_evento_descricao": prazo_data.get("evento_descricao"),
            "prazo_data_envio": prazo_data.get("data_envio"),
            "prazo_inicio": prazo_data.get("prazo_inicio"),
            "prazo_final": prazo_data.get("prazo_final"),
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="cnj").execute()
        processo_id = result.data[0]["id"]

        print(f"  Processo inserido: {cnj} (id={processo_id})")
        print(f"  Assuntos: {len(assuntos)} | Partes: {len(partes)} | Lado: {lado or '(não identificado)'}")

        # Eventos + documentos
        eventos = await extract_eventos(proc_page)
        print(f"  Eventos: {len(eventos)}")

        for e in eventos:
            evento_result = sb.table("eventos").upsert({
                "processo_id": processo_id,
                "numero_evento": e["numero"],
                "data_hora": e["data_hora"],
                "descricao": e["descricao"],
                "usuario": e.get("usuario"),
                "tem_prazo": e.get("tem_prazo", False),
                "prazo_dias": e.get("prazo_dias"),
                "prazo_status": e.get("prazo_status"),
                "prazo_data_inicial": e.get("prazo_data_inicial"),
                "prazo_data_final": e.get("prazo_data_final"),
                "evento_referencia": e.get("evento_referencia"),
                "urgente": e.get("urgente", False),
            }, on_conflict="processo_id,numero_evento").execute()
            evento_id = evento_result.data[0]["id"]

            # Download de documentos deste evento
            for doc in e.get("documentos", []):
                await _download_and_upload(
                    context, sb, processo_id, evento_id,
                    e["numero"], cnj, doc, stats
                )

    finally:
        await proc_page.close()


async def _update_process(context, page, sb, cnj, prazo_data, stats):
    """Atualiza campos de prazo e busca eventos novos."""
    result = sb.table("processos").select("id").eq("cnj", cnj).execute()
    if not result.data:
        return
    processo_id = result.data[0]["id"]

    # Atualizar campos de prazo
    sb.table("processos").update({
        "prazo_evento_descricao": prazo_data.get("evento_descricao"),
        "prazo_data_envio": prazo_data.get("data_envio"),
        "prazo_inicio": prazo_data.get("prazo_inicio"),
        "prazo_final": prazo_data.get("prazo_final"),
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", processo_id).execute()

    # Buscar ultimo evento conhecido
    max_evt = sb.table("eventos") \
        .select("numero_evento") \
        .eq("processo_id", processo_id) \
        .order("numero_evento", desc=True) \
        .limit(1) \
        .execute()
    last_known = max_evt.data[0]["numero_evento"] if max_evt.data else 0

    # Abrir pagina do processo
    proc_page = await open_process_page(context, page, prazo_data["proc_href"])

    try:
        eventos = await extract_eventos(proc_page)
        new_eventos = [e for e in eventos if e["numero"] > last_known]

        if new_eventos:
            print(f"  {len(new_eventos)} eventos novos (> {last_known})")

        for e in new_eventos:
            evento_result = sb.table("eventos").upsert({
                "processo_id": processo_id,
                "numero_evento": e["numero"],
                "data_hora": e["data_hora"],
                "descricao": e["descricao"],
                "usuario": e.get("usuario"),
                "tem_prazo": e.get("tem_prazo", False),
                "prazo_dias": e.get("prazo_dias"),
                "prazo_status": e.get("prazo_status"),
                "prazo_data_inicial": e.get("prazo_data_inicial"),
                "prazo_data_final": e.get("prazo_data_final"),
                "evento_referencia": e.get("evento_referencia"),
                "urgente": e.get("urgente", False),
            }, on_conflict="processo_id,numero_evento").execute()
            evento_id = evento_result.data[0]["id"]

            for doc in e.get("documentos", []):
                await _download_and_upload(
                    context, sb, processo_id, evento_id,
                    e["numero"], cnj, doc, stats
                )
    finally:
        await proc_page.close()


async def _download_and_upload(context, sb, processo_id, evento_id, num_evento, cnj, doc_info, stats):
    """Baixa documento do eProc, sobe para Storage, registra na DB."""
    try:
        doc_result = await download_document(context, doc_info["url_eproc"])
        if not doc_result:
            return

        # Usar extensão real do arquivo baixado
        ext = os.path.splitext(doc_result["local_path"])[1] or ".pdf"
        storage_path = build_storage_path(cnj, num_evento, doc_info["nome"], ext=ext)
        storage_url = upload_document(doc_result["local_path"], storage_path)

        sb.table("documentos").upsert({
            "processo_id": processo_id,
            "evento_id": evento_id,
            "numero_evento": num_evento,
            "nome_original": doc_info["nome"],
            "tipo": doc_result["tipo"],
            "url_eproc": doc_info["url_eproc"],
            "storage_path": storage_path,
            "storage_url": storage_url,
            "tamanho_bytes": doc_result["tamanho_bytes"],
            "hash_sha256": doc_result["hash_sha256"],
        }, on_conflict="processo_id,numero_evento,url_eproc").execute()

        stats["docs_uploaded"] += 1
        print(f"    doc: {doc_info['nome']} -> ok ({doc_result['tamanho_bytes']} bytes)")

    except Exception as e:
        print(f"    doc: {doc_info['nome']} -> ERRO: {e}")


def _create_sync_log(sb) -> str:
    result = sb.table("sync_log").insert({
        "status": "running",
    }).execute()
    return result.data[0]["id"]


def _finish_sync_log(sb, log_id, status, stats, error=None):
    try:
        sb.table("sync_log").update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "processos_added": stats["added"],
            "processos_removed": stats["removed"],
            "processos_updated": stats["updated"],
            "documentos_uploaded": stats["docs_uploaded"],
            "error_message": error[:500] if error else None,
        }).eq("id", log_id).execute()
    except Exception as e:
        print(f"[SYNC] Falha ao gravar sync_log: {e}")
