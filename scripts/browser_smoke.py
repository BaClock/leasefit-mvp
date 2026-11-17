"""Shared-catalog browser acceptance on an isolated fictional DEBUG database."""

import argparse
import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--invite", required=True)
parser.add_argument("--executable", default=None)
parser.add_argument("--output", default="artifacts")
args = parser.parse_args()
base = args.base_url.rstrip("/")
out = Path(args.output)
out.mkdir(parents=True, exist_ok=True)
tag = secrets.token_hex(4)
today = datetime.now(ZoneInfo("Asia/Novosibirsk")).date().isoformat()
with sync_playwright() as pw:
    browser = pw.chromium.launch(
        executable_path=args.executable, headless=True, args=["--no-sandbox"]
    )
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000}, accept_downloads=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base + args.invite)
    password = secrets.token_urlsafe(25)
    for name, value in {
        "name": "Учебный риелтор каталога",
        "new_password1": password,
        "new_password2": password,
    }.items():
        page.locator("#id_" + name).fill(value)
    page.get_by_role("button", name="Активировать и войти").click()
    page.wait_for_url("**/app/requests")

    def capture(name):
        for width in [1440, 768, 390]:
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("window.scrollTo(0, 0)")
            assert page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            ), f"{name} overflows at {width}"
            page.screenshot(
                path=str(out / f"catalog-{name}-{width}.png"), full_page=True
            )
        page.set_viewport_size({"width": 1440, "height": 1000})

    def enter_power(value, source, shared=False):
        form = page.locator("#evidence-form form").first
        form.locator("#id_characteristic").select_option(label="Электрическая мощность")
        for name, v in {
            "value": str(value),
            "source": source,
            "observed_on": today,
            "confirmation": "Условный акт на конкретное помещение",
        }.items():
            form.locator("#id_" + name).fill(v)
        form.locator("#id_source_type").select_option("document")
        form.locator("#id_status").select_option("confirmed")
        if shared:
            form.locator("[name=confirm_shared]").check()
        form.get_by_role("button", name="Сохранить сведения").click()
        expect(page.get_by_role("status").first).to_contain_text("Сведения сохранены")

    ids = []
    for label, power in [("Общий вариант А", 15), ("Общий вариант Б", 12)]:
        page.goto(base + "/app/premises/new")
        commercial = page.locator("details").filter(
            has=page.get_by_text("Коммерческие условия и источники", exact=True)
        )
        commercial.locator("summary").click()
        for name, value in {
            "label": f"{label} {tag}",
            "area": 95,
            "rent": 60000,
            "operating": 8000,
            "utilities": 5000,
            "other_monthly": 0,
            "deposit": 60000,
            "commission": 20000,
        }.items():
            page.locator("#id_" + name).fill(str(value))
        page.locator("#id_premises_type").select_option("retail")
        page.locator("#id_vat_mode").select_option("included")
        page.locator("#id_commercial_source").fill("Условное предложение собственника")
        page.locator("#id_commercial_date").fill(today)
        page.get_by_role("button", name="Сохранить общую карточку").click()
        page.wait_for_url(re.compile(r"/app/premises/\d+$"))
        ids.append(page.url.rsplit("/", 1)[1])
        enter_power(power, "Первый условный акт")
    page.goto(base + "/app/premises?q=" + tag)
    capture("list")
    expect(page.locator("main")).to_contain_text("Общий вариант А")
    expect(page.locator("main")).to_contain_text("Общий вариант Б")

    def create_request(name, power, add_area=False):
        page.goto(base + "/app/requests/new")
        for key, val in {
            "title": f"{name} {tag}",
            "city": "Новосибирск",
            "business": name,
            "budget": "70000",
            "power_kw_min": str(power),
        }.items():
            page.locator("#id_" + key).fill(val)
        page.locator("#id_power_kw_priority").select_option("required")
        if add_area:
            page.locator("#id_area_sqm_min").fill("50")
            page.locator("#id_area_sqm_max").fill("110")
            page.locator("#id_area_sqm_priority").select_option("desired")
            capture("requirements")
        page.get_by_role("button", name="Далее: уточнить профиль").click()
        page.wait_for_url(re.compile(r"/requests/\d+/refine$"))
        req_id = page.url.split("/")[-2]
        page.get_by_role("button", name="Далее: помещения").click()
        page.wait_for_url(re.compile(r"/requests/\d+/objects$"))
        page.get_by_role("link", name="Выбрать из каталога").click()
        return req_id

    coffee = create_request("Кофейня", 25, add_area=True)
    for p_id in ids:
        page.locator(f'input[name=objects][value="{p_id}"]').check()
    page.get_by_role("button", name="Добавить выбранные без копирования").click()
    page.goto(base + f"/app/requests/{coffee}/objects/{ids[0]}")
    form = page.locator(f'form[action$="/{ids[0]}/context"]')
    for key, value in {
        "adaptation": 90000,
        "other_once": 0,
        "stress": 5000,
        "internal_notes": "PRIVATE BROWSER COFFEE NOTE",
    }.items():
        form.locator("#id_" + key).fill(str(value))
    form.get_by_role("button", name="Сохранить условия запроса").click()
    page.goto(base + f"/app/requests/{coffee}/compare")
    page.get_by_role("button", name="↻ Проверить / пересчитать выбранные").click()
    expect(page.locator("main")).to_contain_text("Не соответствует")
    expect(page.locator("main")).to_contain_text("Превышение 3 000")
    page.locator(
        '.report-parameters button[data-parameter="economics:monthly"]'
    ).click()
    capture("comparison")
    page.get_by_role("button", name="Сформировать отчёт").click()
    with page.expect_download() as event:
        page.get_by_role("link", name="Скачать PDF").first.click()
    original = out / "LeaseFIT-shared-catalog-example.pdf"
    event.value.save_as(str(original))
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    pickup = create_request("ПВЗ", 15)
    page.locator(f'input[name=objects][value="{ids[0]}"]').check()
    page.get_by_role("button", name="Добавить выбранные без копирования").click()
    page.goto(base + f"/app/requests/{pickup}/compare")
    page.get_by_role("button", name="↻ Проверить / пересчитать выбранные").click()
    expect(page.locator("main")).to_contain_text("Соответствует")
    page.goto(base + f"/app/premises/{ids[0]}")
    expect(page.locator("main")).not_to_contain_text("PRIVATE BROWSER COFFEE NOTE")
    expect(page.locator("main")).to_contain_text(
        "Помещение используется в нескольких запросах"
    )
    enter_power(30, "Новый общий акт", shared=True)
    history = page.locator(".history-item").filter(has_text="Новый общий акт")
    history.locator("xpath=ancestor::details[1]").locator("summary").click()
    history.locator("input[name=reason]").fill("Новый акт заменяет прежние сведения")
    history.locator("[name=confirm_shared]").check()
    history.get_by_role("button", name="Выбрать источник").click()
    for req in [coffee, pickup]:
        page.goto(base + f"/app/requests/{req}/compare")
        expect(page.locator("main")).to_contain_text("Не проверено — пересчитайте")
    page.goto(base + f"/app/requests/{pickup}/compare")
    page.get_by_role("button", name="↻ Проверить / пересчитать выбранные").click()
    expect(page.locator("main")).to_contain_text("Соответствует")
    page.goto(base + f"/app/requests/{pickup}/objects")
    page.get_by_role("button", name="Убрать из запроса").click()
    page.goto(base + f"/app/premises/{ids[0]}")
    expect(page.get_by_role("heading", name=f"Общий вариант А {tag}")).to_be_visible()
    page.goto(base + f"/app/requests/{coffee}/compare")
    with page.expect_download() as event:
        page.get_by_role("link", name="Скачать PDF").first.click()
    target = out / "LeaseFIT-after-shared-change.pdf"
    event.value.save_as(str(target))
    assert hashlib.sha256(target.read_bytes()).hexdigest() == original_hash
    assert not errors, errors
    context.close()
    browser.close()
print(
    "PASS: independent catalog creation → grouped requirements → shared use in coffee/PVZ with different results → common update and stale evaluations → unlink without deleting → unchanged issued PDF; 1440/768/390 px; no JS errors or page overflow."
)
