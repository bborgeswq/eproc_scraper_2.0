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
    """Sync linear: scrapeia tudo, salva tudo, sem limites."""
    sb = get_supabase()
    log_id = _start_log(sb)
    stats = {"total": 0, "novos": 0, "removidos": 0, "docs": 0, "erros": 0}

    try:
        # 1. Scrapear prazos abertos do eProc
        eproc = await scrape_prazos_abertos(page)
        eproc_cnjs = set(eproc.keys())
        stats["total"] = len(eproc_cnjs)

        # 2. CNJs na DB
        db_rows = sb.table("processos").select("cnj").execute()
        db_cnjs = {row["cnj"] for row in db_rows.data}

        to_add = eproc_cnjs - db_cnjs
        to_remove = db_cnjs - eproc_cnjs

        print(f"\n[SYNC] {len(eproc_cnjs)} processos no eProc | +{len(to_add)} novos | -{len(to_remove)} removidos | {len(eproc_cnjs & db_cnjs)} mantidos")

        # 3. Remover processos que saíram (com proteção)
        if len(eproc_cnjs) == 0 and len(db_cnjs) > 0:
            print(f"[SYNC] AVISO: eProc retornou 0 processos mas DB tem {len(db_cnjs)}. Pulando remoção.")
            to_remove = set()

        for cnj in to_remove:
            print(f"[SYNC] Removendo: {cnj}")
            delete_process_documents(cnj)
            sb.table("processos").delete().eq("cnj", cnj).execute()
            stats["removidos"] += 1

        # 4. Sync rápido: inserir novos + atualizar prazos de TODOS
        for cnj, prazos_list in eproc.items():
            first = prazos_list[0]
            sb.table("processos").upsert({
                "cnj": cnj,
                "classe": first.get("classe"),
                "juizo": first.get("juizo"),
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="cnj").execute()

            # Sync prazos_abertos
            sb.table("prazos_abertos").delete().eq("cnj", cnj).execute()
            for p in prazos_list:
                sb.table("prazos_abertos").insert({
                    "cnj": cnj,
                    "evento_descricao": p.get("evento_descricao", ""),
                    "data_envio": p.get("data_envio"),
                    "prazo_inicio": p.get("prazo_inicio"),
                    "prazo_final": p.get("prazo_final"),
                }).execute()

            if cnj in to_add:
                stats["novos"] += 1

        total_prazos = sum(len(v) for v in eproc.values())
        print(f"[SYNC] Prazos sincronizados: {total_prazos} prazos para {len(eproc)} processos")

        # 5. Scrape completo de cada processo
        for i, (cnj, prazos_list) in enumerate(eproc.items(), 1):
            first = prazos_list[0]
            print(f"\n[SYNC] [{i}/{len(eproc)}] Processando: {cnj}")

            try:
                await _scrape_full_process(context, page, sb, cnj, first["proc_href"], stats)
            except Exception as e:
                print(f"[SYNC] ERRO em {cnj}: {e}")
                stats["erros"] += 1

            await asyncio.sleep(1)

        status = "success" if stats["erros"] == 0 else "partial"
        _finish_log(sb, log_id, status, stats)
        print(f"\n[SYNC] Concluído! {stats['total']} processos | {stats['docs']} docs | {stats['erros']} erros")
        return stats

    except Exception as e:
        _finish_log(sb, log_id, "error", stats, str(e))
        print(f"[SYNC] ERRO FATAL: {e}")
        raise


async def _scrape_full_process(context, page, sb, cnj, proc_href, stats):
    """Abre processo, extrai tudo, salva na DB."""
    proc_page = await open_process_page(context, page, proc_href)

    try:
        # Header
        header = await extract_header(proc_page)

        # Partes e assuntos
        assuntos = await extract_assuntos(proc_page)
        partes = await extract_partes(proc_page)
        lado = identify_adv_side(partes, Config.ADV_NAME)

        # Atualizar processo com dados completos
        sb.table("processos").update({
            "classe": header.get("classe"),
            "competencia": header.get("competencia"),
            "data_autuacao": header.get("data_autuacao"),
            "situacao": header.get("situacao"),
            "orgao_julgador": header.get("orgao_julgador"),
            "juiz": header.get("juiz"),
            "lado_advogado": lado,
            "processos_relacionados": header.get("processos_relacionados", []),
            "assuntos": assuntos,
            "partes": partes,
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }).eq("cnj", cnj).execute()

        print(f"  Header: {header.get('classe')} | Partes: {len(partes)} | Lado: {lado or '?'}")

        # Eventos
        eventos = await extract_eventos(proc_page)

        # Filtrar apenas eventos novos (que não estão na DB)
        max_evt = sb.table("eventos") \
            .select("numero_evento") \
            .eq("cnj", cnj) \
            .order("numero_evento", desc=True) \
            .limit(1) \
            .execute()
        last_known = max_evt.data[0]["numero_evento"] if max_evt.data else 0
        new_eventos = [e for e in eventos if e["numero"] > last_known]

        print(f"  Eventos: {len(eventos)} total | {len(new_eventos)} novos (> {last_known})")

        for e in new_eventos:
            sb.table("eventos").upsert({
                "cnj": cnj,
                "numero_evento": e["numero"],
                "data_hora": e["data_hora"],
                "descricao": e["descricao"],
                "usuario": e.get("usuario"),
                "prazo_aberto": e.get("prazo_aberto", False),
                "prazo_dias": e.get("prazo_dias"),
                "prazo_status": e.get("prazo_status"),
                "prazo_data_inicial": e.get("prazo_data_inicial"),
                "prazo_data_final": e.get("prazo_data_final"),
                "evento_referencia": e.get("evento_referencia"),
                "urgente": e.get("urgente", False),
            }, on_conflict="cnj,numero_evento").execute()

            # Download de documentos
            for doc in e.get("documentos", []):
                await _download_and_upload(context, sb, cnj, e["numero"], doc, stats)

    finally:
        await proc_page.close()


async def _download_and_upload(context, sb, cnj, num_evento, doc_info, stats):
    """Baixa documento do eProc, sobe para Storage, registra na DB."""
    try:
        doc_result = await download_document(context, doc_info["url_eproc"])
        if not doc_result:
            return

        ext = os.path.splitext(doc_result["local_path"])[1] or ".pdf"
        storage_path = build_storage_path(cnj, num_evento, doc_info["nome"], ext=ext)
        storage_url = upload_document(doc_result["local_path"], storage_path)

        sb.table("documentos").upsert({
            "cnj": cnj,
            "numero_evento": num_evento,
            "nome_original": doc_info["nome"],
            "tipo": doc_result["tipo"],
            "url_eproc": doc_info["url_eproc"],
            "storage_path": storage_path,
            "storage_url": storage_url,
            "tamanho_bytes": doc_result["tamanho_bytes"],
            "hash_sha256": doc_result["hash_sha256"],
        }, on_conflict="cnj,numero_evento,url_eproc").execute()

        stats["docs"] += 1
        print(f"    doc: {doc_info['nome']} -> ok ({doc_result['tamanho_bytes']} bytes)")

    except Exception as e:
        print(f"    doc: {doc_info['nome']} -> ERRO: {e}")


def _start_log(sb) -> str:
    result = sb.table("sync_log").insert({"status": "running"}).execute()
    return result.data[0]["id"]


def _finish_log(sb, log_id, status, stats, error=None):
    try:
        sb.table("sync_log").update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "processos_total": stats["total"],
            "processos_novos": stats["novos"],
            "processos_removidos": stats["removidos"],
            "documentos_baixados": stats["docs"],
            "erros": stats["erros"],
            "error_message": error[:500] if error else None,
        }).eq("id", log_id).execute()
    except Exception as e:
        print(f"[SYNC] Falha ao gravar sync_log: {e}")
