"""Verify the comparison workflow in a freshly seeded disposable showcase."""

import argparse
import os
import re

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--request-id", type=int, required=True)
    args = parser.parse_args()
    request_url = f"{args.base_url}/app/requests/{args.request_id}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(f"{request_url}/objects")
        page.locator("#id_username").fill("novosibirsk-showcase@example.invalid")
        page.locator("#id_password").fill(os.environ["LEASEFIT_BROWSER_PASSWORD"])
        page.get_by_role("button", name="Войти").click()
        cards = page.locator(".recommendation-card")
        expect(cards).to_have_count(20)
        expect(
            page.get_by_text("Выберите интересующие помещения из рекомендаций")
        ).to_be_visible()
        first = cards.filter(has_text="NSK-P01")
        expect(first).to_contain_text("18,00 кВт")
        expect(first).to_contain_text("Превышение 8 000")
        expect(first).to_contain_text("Соответствует")
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            expect(first).to_be_visible()
        first.get_by_role("link", name="Открыть карточку").click()
        expect(page.locator("main")).to_contain_text("NSK-P01")
        page.go_back()
        for code in ("NSK-P01", "NSK-P02"):
            cards.filter(has_text=code).get_by_role(
                "button", name="Добавить к сравнению"
            ).click()
            expect(
                cards.filter(has_text=code).get_by_text("Уже в сравнении")
            ).to_be_visible()
        selected = page.locator('[id^="selected-"]')
        expect(selected).to_have_count(2)
        selected.filter(has_text="NSK-P01").get_by_role(
            "link", name="Финансовый анализ"
        ).click()
        expect(page.locator("main")).to_contain_text("102 000 ₽")
        page.get_by_role("link", name="Настроить финансовый сценарий").click()
        expect(page.locator("#id_average")).to_have_value("320")
        page.goto(f"{request_url}/compare")
        page.get_by_role("button", name="↻ Проверить / пересчитать выбранные").click()
        expect(page.locator("main")).to_contain_text(
            re.compile(r"1\s?103 операций в месяц")
        )
        page.get_by_role("button", name="Сформировать отчёт").click()
        with page.expect_download() as downloaded:
            page.get_by_role("link", name="Скачать PDF").first.click()
        assert downloaded.value.path().read_bytes().startswith(b"%PDF-")
        page.goto(f"{request_url}/objects")
        selected.filter(has_text="NSK-P01").get_by_role(
            "button", name="Убрать из сравнения"
        ).click()
        expect(selected).to_have_count(1)
        expect(first.get_by_role("button", name="Добавить к сравнению")).to_be_visible()
        assert not errors, errors
        browser.close()
    print("PASS: каталог, подбор, сравнение, финансовый сценарий и PDF; 1440 и 390 px.")


if __name__ == "__main__":
    main()
