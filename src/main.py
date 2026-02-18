import sys
import asyncio
from playwright.async_api import async_playwright, Playwright
from src.config import Config
from src.auth.login import login
from src.db.sync import sync

# Windows console: forçar UTF-8 para evitar erros com caracteres especiais
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Intervalo entre ciclos completos (24 horas em segundos)
WAIT_AFTER_COMPLETE = 24 * 60 * 60
# Intervalo entre iterações do loop quando há processos pendentes (30 segundos)
WAIT_BETWEEN_ITERATIONS = 30


def _build_proxy():
    """Constrói dict de proxy a partir das configs."""
    if not Config.PROXY_SERVER:
        return None
    proxy = {"server": Config.PROXY_SERVER}
    if Config.PROXY_USERNAME:
        proxy["username"] = Config.PROXY_USERNAME
        proxy["password"] = Config.PROXY_PASSWORD
    return proxy


async def _create_session(p: Playwright, proxy: dict | None):
    """Cria browser + context + login. Retorna (browser, context, page)."""
    browser = await p.chromium.launch(headless=Config.HEADLESS)
    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        proxy=proxy,
    )
    page = await login(context)
    return browser, context, page


async def _close_session(browser, context):
    """Fecha context e browser de forma segura."""
    try:
        await context.close()
    except Exception:
        pass
    try:
        await browser.close()
    except Exception:
        pass


async def run():
    Config.validate()

    print("=" * 60)
    print("  eProc TJRS Scraper 2.0 - Sync")
    print("=" * 60)

    production_mode = Config.PROCESS_LIMIT > 0
    if production_mode:
        print(f"[MODO PRODUÇÃO] PROCESS_LIMIT={Config.PROCESS_LIMIT} — loop contínuo até completar tudo")
    else:
        print("[MODO LOCAL] PROCESS_LIMIT=0 — processar tudo de uma vez")

    proxy = _build_proxy()
    if proxy:
        print(f"[PROXY] Usando proxy: {proxy['server']}")

    async with async_playwright() as p:
        browser, context, page = await _create_session(p, proxy)
        print("[OK] Logado no Painel do Advogado\n")

        if not production_mode:
            # Modo local: sync único
            await sync(page, context)
            await browser.close()
            print("\n[OK] Browser fechado. Sync finalizado.")
            return

        # Modo produção: loop contínuo
        iteration = 0
        while True:
            iteration += 1
            print(f"\n{'='*60}")
            print(f"  ITERAÇÃO #{iteration}")
            print(f"{'='*60}")

            try:
                stats = await sync(page, context)
            except Exception as e:
                print(f"[LOOP] Erro no sync: {e}")
                print(f"[LOOP] Recriando sessão em {WAIT_BETWEEN_ITERATIONS}s...")
                await asyncio.sleep(WAIT_BETWEEN_ITERATIONS)
                # Recriar browser inteiro (pode ter crashado)
                try:
                    await _close_session(browser, context)
                    browser, context, page = await _create_session(p, proxy)
                    print("[OK] Sessão recriada com sucesso")
                except Exception as session_err:
                    print(f"[LOOP] Falha ao recriar sessão: {session_err}")
                    print("[LOOP] Aguardando 60s antes de tentar novamente...")
                    await asyncio.sleep(60)
                continue

            has_pending = stats.get("has_pending", False)
            has_errors = stats.get("errors", 0) > 0

            if has_pending or has_errors:
                wait = WAIT_BETWEEN_ITERATIONS
                reason = "processos pendentes" if has_pending else "erros no sync"
                print(f"\n[LOOP] Ainda há {reason}. Próxima iteração em {wait}s...")
                await asyncio.sleep(wait)
            else:
                # Tudo completo! Fechar browser e aguardar 24h
                hours = WAIT_AFTER_COMPLETE / 3600
                print(f"\n[LOOP] Todos os processos completos! Próximo sync em {hours:.0f}h...")
                await _close_session(browser, context)
                await asyncio.sleep(WAIT_AFTER_COMPLETE)
                # Recriar sessão completa após 24h
                try:
                    browser, context, page = await _create_session(p, proxy)
                    print("[OK] Sessão recriada após espera de 24h")
                except Exception as session_err:
                    print(f"[LOOP] Falha ao recriar sessão: {session_err}")
                    break


if __name__ == "__main__":
    asyncio.run(run())
