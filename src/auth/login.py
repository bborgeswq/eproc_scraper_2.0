import asyncio
import re
import pyotp
from playwright.async_api import Page, BrowserContext
from src.config import Config


def _clean_totp_secret(secret: str) -> str:
    """Remove espaços, hífens e caracteres inválidos do TOTP secret."""
    return re.sub(r"[^A-Za-z2-7=]", "", secret)


async def login(context: BrowserContext) -> Page:
    """
    Autentica no eProc TJRS via Keycloak SSO + TOTP.
    Retorna a page autenticada.
    """
    page = await context.new_page()

    print("[LOGIN] Navegando para o eProc...")
    await page.goto(Config.EPROC_LOGIN_URL, wait_until="networkidle")

    # Verificar se foi redirecionado para Keycloak
    current_url = page.url
    if "keycloak" in current_url or "login" in current_url.lower():
        print("[LOGIN] Tela de login Keycloak detectada")
        await _fill_credentials(page)
        await _handle_2fa(page)
    else:
        print("[LOGIN] Já autenticado (sessão ativa)")

    # Confirmar que estamos no eProc autenticado
    await page.wait_for_url(f"{Config.EPROC_BASE_URL}/**", timeout=30000)
    print(f"[LOGIN] Autenticado com sucesso! URL: {page.url}")

    return page


async def _fill_credentials(page: Page):
    """Preenche usuário e senha no form do Keycloak."""
    print("[LOGIN] Preenchendo credenciais...")

    # Aguardar o formulário de login
    await page.wait_for_selector("#username", timeout=15000)

    await page.fill("#username", Config.EPROC_USERNAME)
    await page.fill("#password", Config.EPROC_PASSWORD)

    print("[LOGIN] Submetendo formulário...")
    await page.click("#kc-login")

    # Aguardar a próxima página (2FA ou redirect)
    await asyncio.sleep(2)


async def _handle_2fa(page: Page):
    """Detecta e preenche o código TOTP se a tela de 2FA aparecer."""
    # Aguardar um pouco para a página carregar
    await asyncio.sleep(2)

    current_url = page.url
    page_content = await page.content()

    # Verificar se estamos numa tela de 2FA/OTP
    is_2fa = (
        "otp" in current_url.lower()
        or "totp" in current_url.lower()
        or "2fa" in current_url.lower()
        or "otp" in page_content.lower()
    )

    if not is_2fa:
        print("[LOGIN] Sem tela de 2FA detectada, prosseguindo...")
        return

    print("[LOGIN] Tela de 2FA detectada, gerando código TOTP...")

    # Gerar código TOTP
    clean_secret = _clean_totp_secret(Config.TOTP_SECRET)
    totp = pyotp.TOTP(clean_secret)
    code = totp.now()
    print(f"[LOGIN] Código TOTP gerado: {code}")

    # Tentar encontrar o campo de input do OTP
    # Keycloak normalmente usa #otp ou input[name="otp"]
    otp_selectors = [
        "#otp",
        "input[name='otp']",
        "input[name='totp']",
        "input[id='otp']",
        "input[type='text']",
    ]

    for selector in otp_selectors:
        try:
            element = page.locator(selector)
            if await element.count() > 0:
                print(f"[LOGIN] Campo OTP encontrado: {selector}")
                await element.fill(code)
                break
        except Exception:
            continue
    else:
        print("[LOGIN] AVISO: Não encontrei o campo OTP automaticamente.")
        print("[LOGIN] Por favor, insira o código manualmente no browser.")
        input("Pressione Enter após inserir o código 2FA manualmente...")
        return

    # Submeter o formulário de 2FA
    submit_selectors = [
        "#kc-login",
        "input[type='submit']",
        "button[type='submit']",
    ]

    for selector in submit_selectors:
        try:
            element = page.locator(selector)
            if await element.count() > 0:
                await element.click()
                print("[LOGIN] Formulário 2FA submetido")
                break
        except Exception:
            continue

    # Aguardar redirect
    await asyncio.sleep(3)
