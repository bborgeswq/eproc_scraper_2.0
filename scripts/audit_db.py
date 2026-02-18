"""Audit script: verifica dados extraídos no Supabase."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.client import get_supabase

def main():
    sb = get_supabase()

    # === PROCESSOS ===
    procs = sb.table("processos").select("*").execute().data
    print(f"\n{'='*60}")
    print(f"  PROCESSOS: {len(procs)}")
    print(f"{'='*60}")

    lados = {"AUTOR": 0, "RÉU": 0, "": 0}
    for p in procs:
        lado = p.get("lado_advogado") or ""
        lados[lado] = lados.get(lado, 0) + 1
        print(f"\n  CNJ: {p['cnj']}  [{lado or 'sem lado'}]")
        print(f"  Classe: {p.get('classe') or '(vazio)'}")
        print(f"  Competência: {p.get('competencia') or '(vazio)'}")
        print(f"  Data Autuação: {p.get('data_autuacao') or '(vazio)'}")
        print(f"  Situação: {p.get('situacao') or '(vazio)'}")
        print(f"  Órgão Julgador: {p.get('orgao_julgador') or '(vazio)'}")
        print(f"  Juiz: {p.get('juiz') or '(vazio)'}")
        print(f"  Juízo: {p.get('juizo') or '(vazio)'}")
        print(f"  Proc. Relacionados: {p.get('processos_relacionados') or '(vazio)'}")

        assuntos = p.get("assuntos") or []
        partes = p.get("partes") or []
        # Handle double-serialized JSONB (string instead of list)
        if isinstance(assuntos, str):
            print(f"  *** ASSUNTOS DOUBLE-SERIALIZED (string) ***")
        else:
            print(f"  Assuntos ({len(assuntos)}):")
            for a in assuntos:
                print(f"    - {a}")
        if isinstance(partes, str):
            print(f"  *** PARTES DOUBLE-SERIALIZED (string) ***")
        else:
            print(f"  Partes ({len(partes)}):")
            for pt in partes:
                qual = pt.get('qualificacao', '')
                qual_str = f" [{qual}]" if qual else ""
                reps = pt.get('representantes', [])
                reps_str = ", ".join(f"{r.get('nome','')} ({r.get('oab','')}/{r.get('tipo','')})" for r in reps) if reps else "sem advogado"
                print(f"    - {pt.get('tipo','?')}: {pt.get('nome','?')} ({pt.get('cpf_cnpj','sem doc')}){qual_str} -> {reps_str}")

        print(f"  Prazo evento: {p.get('prazo_evento_descricao') or '(vazio)'}")
        print(f"  Prazo envio: {p.get('prazo_data_envio') or '(vazio)'}")
        print(f"  Prazo início: {p.get('prazo_inicio') or '(vazio)'}")
        print(f"  Prazo final: {p.get('prazo_final') or '(vazio)'}")

    # Resumo de lados
    print(f"\n  --- Resumo lados: AUTOR={lados.get('AUTOR',0)} | RÉU={lados.get('RÉU',0)} | sem lado={lados.get('',0)} ---")

    # === EVENTOS ===
    for p in procs:
        pid = p["id"]
        eventos = sb.table("eventos").select("*").eq("processo_id", pid).order("numero_evento").execute().data
        print(f"\n{'='*60}")
        print(f"  EVENTOS de {p['cnj']}: {len(eventos)}")
        print(f"{'='*60}")

        campos_vazios = {"usuario": 0, "tem_prazo_true": 0, "prazo_dias": 0, "evento_referencia": 0, "urgente_true": 0}
        for e in eventos:
            if e.get("usuario"): campos_vazios["usuario"] += 1
            if e.get("tem_prazo"): campos_vazios["tem_prazo_true"] += 1
            if e.get("prazo_dias"): campos_vazios["prazo_dias"] += 1
            if e.get("evento_referencia"): campos_vazios["evento_referencia"] += 1
            if e.get("urgente"): campos_vazios["urgente_true"] += 1

        print(f"  Com usuário: {campos_vazios['usuario']}/{len(eventos)}")
        print(f"  Com prazo: {campos_vazios['tem_prazo_true']}")
        print(f"  Com prazo_dias: {campos_vazios['prazo_dias']}")
        print(f"  Com evento_referencia: {campos_vazios['evento_referencia']}")
        print(f"  Urgentes: {campos_vazios['urgente_true']}")

        # Mostrar primeiros e últimos eventos
        if eventos:
            print(f"\n  Primeiro: evt {eventos[0]['numero_evento']} - {eventos[0]['data_hora'][:10]} - {eventos[0]['descricao'][:60]}")
            print(f"  Último:   evt {eventos[-1]['numero_evento']} - {eventos[-1]['data_hora'][:10]} - {eventos[-1]['descricao'][:60]}")

    # === DOCUMENTOS ===
    for p in procs:
        pid = p["id"]
        docs = sb.table("documentos").select("*").eq("processo_id", pid).execute().data
        print(f"\n{'='*60}")
        print(f"  DOCUMENTOS de {p['cnj']}: {len(docs)}")
        print(f"{'='*60}")

        tipos = {}
        total_bytes = 0
        sem_storage = 0
        for d in docs:
            t = d.get("tipo") or "?"
            tipos[t] = tipos.get(t, 0) + 1
            total_bytes += d.get("tamanho_bytes") or 0
            if not d.get("storage_url"):
                sem_storage += 1

        print(f"  Tipos: {tipos}")
        print(f"  Tamanho total: {total_bytes / 1024 / 1024:.1f} MB")
        print(f"  Sem storage_url: {sem_storage}")

    # === SYNC LOG ===
    logs = sb.table("sync_log").select("*").order("started_at", desc=True).limit(5).execute().data
    print(f"\n{'='*60}")
    print(f"  SYNC LOG (últimos 5)")
    print(f"{'='*60}")
    for log in logs:
        print(f"  {log['started_at'][:19]} | {log['status']} | +{log.get('processos_added',0)} -{log.get('processos_removed',0)} ~{log.get('processos_updated',0)} docs:{log.get('documentos_uploaded',0)}")
        if log.get("error_message"):
            print(f"    ERRO: {log['error_message'][:100]}")


if __name__ == "__main__":
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
