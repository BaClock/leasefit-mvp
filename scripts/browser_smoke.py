"""Real Chromium flow against an isolated DEBUG=1 database. Uses only fictional data.
Usage: python scripts/browser_smoke.py --base-url http://127.0.0.1:8010 --invite /invite/<token>
"""

import argparse, re, secrets
from pathlib import Path
from datetime import date
from playwright.sync_api import sync_playwright, expect

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://127.0.0.1:8000")
p.add_argument("--invite", required=True)
p.add_argument("--executable", default=None)
p.add_argument("--output", default="artifacts")
args = p.parse_args()
base = args.base_url.rstrip("/")
out = Path(args.output)
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch(
        executable_path=args.executable, headless=True, args=["--no-sandbox"]
    )
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000}, accept_downloads=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + args.invite)
    expect(page.get_by_role("heading", name="Добро пожаловать")).to_be_visible()
    password = secrets.token_urlsafe(24)
    page.locator("#id_name").fill("Учебный риелтор браузерного теста")
    page.locator("#id_new_password1").fill(password)
    page.locator("#id_new_password2").fill(password)
    page.get_by_role("button", name="Активировать и войти").click()
    page.wait_for_url("**/app/requests")
    page.get_by_role("link", name="＋ Создать запрос").click()
    page.locator("#id_title").fill("Кофейня · тест браузера " + secrets.token_hex(3))
    page.locator("#id_city").fill("Новосибирск")
    page.locator("#id_business").fill("Кофейня")
    page.locator("#id_budget").fill("70000")
    page.locator("#id_requirements-0-name").fill("Мощность")
    page.locator("#id_requirements-0-unit").fill("кВт")
    page.locator("#id_requirements-0-minimum").fill("15")
    for width in [1440, 768, 390]:
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            f"Profile overflows at {width}"
        )
        page.screenshot(path=str(out / f"profile-{width}.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.get_by_role("button", name="Далее: уточнить профиль").click()
    page.wait_for_url(re.compile(r"/app/requests/\d+/refine$"))
    assert page.locator("#id_power").count() == 0  # Already set in the profile.
    page.locator("#id_geography").fill("Левый берег")
    page.locator("#id_area_min").fill("50")
    page.locator("#id_area_max").fill("70")
    page.get_by_role("button", name="Далее: помещения").click()
    page.wait_for_url(re.compile(r"/app/requests/\d+/objects$"))
    req_path = page.url.replace(base, "").removesuffix("/objects")
    ids = []
    for label, power in [("Учебный вариант А", 15), ("Учебный вариант Б", 12)]:
        if not ids:
            page.get_by_role("link", name="Помещения", exact=True).click()
            expect(page.get_by_role("heading", name="Все помещения")).to_be_visible()
            page.get_by_role("link", name="＋ Добавить помещение").click()
            page.locator("#id_request_id").select_option(req_path.split("/")[-1])
            page.get_by_role("button", name="Далее: карточка помещения").click()
        else:
            page.goto(base + req_path + "/objects/new")
        for name, value in {
            "label": label,
            "area": 60,
            "rate": 1000,
            "operating": 8000,
            "utilities": 5000,
            "other_monthly": 0,
            "deposit": 60000,
            "commission": 20000,
            "adaptation": 90000,
            "other_once": 0,
            "stress": 5000,
        }.items():
            page.locator("#id_" + name).fill(str(value))
        page.locator("#id_vat_mode").select_option("included")
        page.locator("#id_commercial_source").fill("Вымышленное письмо об условиях")
        page.locator("#id_commercial_date").fill(date.today().isoformat())
        page.get_by_role("button", name="Сохранить карточку").click()
        page.wait_for_url(re.compile(r"/objects/\d+$"))
        ids.append(page.url.rsplit("/", 1)[1])
        for name, value in {
            "name": "Мощность",
            "value": str(power),
            "unit": "кВт",
            "source": "Условный акт подключения",
            "observed_on": date.today().isoformat(),
            "confirmation": "Учебное подтверждение по акту",
        }.items():
            page.locator("#id_" + name).fill(value)
        page.locator("#id_source_type").select_option("document")
        page.locator("#id_status").select_option("confirmed")
        page.get_by_role("button", name="Сохранить сведения").click()
        expect(page.get_by_role("status").first).to_contain_text("Сведения сохранены")
    page.goto(base + req_path + "/compare")
    page.get_by_role("button", name="↻ Проверить / пересчитать выбранные").click()
    expect(page.locator("main")).to_contain_text("Есть подтверждённый блокер")
    expect(page.locator("main")).to_contain_text("Превышение 3 000")
    page.goto(
        base
        + req_path
        + "/compare?"
        + "&".join("objects=" + i for i in ids)
        + "&differences=1"
    )
    for width in [1440, 768, 390]:
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            f"Comparison overflows at {width}"
        )
        expect(
            page.locator(".mobile-compare" if width <= 768 else ".compare-scroll")
        ).to_contain_text("Блокер")
        page.screenshot(path=str(out / f"compare-{width}.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    parameter = page.locator(
        '.report-parameters button[data-parameter^="requirement:"]'
    )
    parameter.click()
    expect(parameter).to_contain_text("В отчёте")
    page.locator(
        '.report-parameters button[data-parameter="economics:monthly"]'
    ).click()
    page.locator('.report-parameters button[data-parameter="future:leads"]').click()
    expect(page.locator(".report-preview")).to_contain_text(
        "Отсеян при текущих условиях"
    )
    page.get_by_role("button", name="Сформировать отчёт").click()
    expect(page.get_by_role("status")).to_contain_text("Клиентская версия 1 сохранена")
    with page.expect_download() as download:
        page.get_by_role("link", name="Скачать PDF").click()
    target = out / "LeaseFIT-v2-browser-example.pdf"
    download.value.save_as(str(target))
    assert target.read_bytes().startswith(b"%PDF-")
    assert not errors, errors
    context.close()
    browser.close()
print(
    "PASS: invitation → profile → refinement → global premises → combined check/compare → parameter selection → PDF download; 1440/768/390 px; no horizontal page overflow or JavaScript errors."
)
