import sys
import asyncio
from playwright.async_api import async_playwright, Playwright
from src.config import Config
from src.auth.login import login
from src.db.sync import sync

# Windows console: forçar UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WAIT_AFTER_COMPLETE = 24 * 60 * 60  # 24h entre syncs


def _build_proxy():
    if not Config.PROXY_SERVER:
        return None
    proxy = {"server": Config.PROXY_SERVER}
    if Config.PROXY_USERNAME:
        proxy["username"] = Config.PROXY_USERNAME
        proxy["password"] = Config.PROXY_PASSWORD
    return proxy


async def _create_session(p: Playwright, proxy: dict | None):
    browser = await p.chromium.launch(headless=Config.HEADLESS)
    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        proxy=proxy,
    )
    page = await login(context)
    return browser, context, page


async def _close_session(browser, context):
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
    print("  eProc TJRS Scraper 2.0")
    print("=" * 60)

    proxy = _build_proxy()
    if proxy:
        print(f"[PROXY] Usando proxy: {proxy['server']}")

    async with async_playwright() as p:
        while True:
            try:
                browser, context, page = await _create_session(p, proxy)
                print("[OK] Logado no Painel do Advogado\n")

                await sync(page, context)

                await _close_session(browser, context)
            except Exception as e:
                print(f"[ERRO] Falha no sync: {e}")
                try:
                    await _close_session(browser, context)
                except Exception:
                    pass

            hours = WAIT_AFTER_COMPLETE / 3600
            print(f"\n[OK] Sync completo. Próximo em {hours:.0f}h...")
            await asyncio.sleep(WAIT_AFTER_COMPLETE)


if __name__ == "__main__":
    asyncio.run(run())
