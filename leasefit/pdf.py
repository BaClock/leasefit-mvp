from io import BytesIO
from xml.sax.saxutils import escape
from django.conf import settings
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT


def render_pdf(s):
    if "LeaseFIT" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("LeaseFIT", settings.PDF_FONT))
    buf = BytesIO()
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "BodyRu",
        fontName="LeaseFIT",
        fontSize=9,
        leading=14,
        spaceAfter=7,
        textColor=colors.HexColor("#17243B"),
        wordWrap="CJK",
    )
    h1 = ParagraphStyle(
        "TitleRu", parent=normal, fontSize=22, leading=28, spaceAfter=16
    )
    h2 = ParagraphStyle(
        "HeadRu", parent=normal, fontSize=14, leading=20, spaceBefore=14
    )
    story = []

    def p(t, style=normal):
        story.append(Paragraph(escape(str(t)).replace("\n", "<br/>"), style))

    p("LeaseFIT · Клиентский отчёт", h1)
    p(
        f"Версия {s['version']} · {s['issued_at'][:19]} (Новосибирск, UTC+7) · Правила {s['rules_version']}"
    )
    p(f"{s['agency']} · {s['author']}")
    p(f"{s['request']} · {s['city']} · {s['business']}", h2)
    p(
        f"Месячный лимит: {s['budget'] or 'неизвестен'} ₽. Бюджет на старт: {s['start_budget'] or 'неизвестен'} ₽."
    )
    if s["geography"]:
        p(s["geography"])
    p("Требования клиента", h2)
    for r in s["requirements"]:
        p(r)
    if s["comment"]:
        p("Комментарий: " + s["comment"])
    for obj in s["objects"]:
        p(obj["title"], h2)
        p(obj["decision"])
        p("Технический статус: " + obj["label"])
        p("Проверка: " + obj["checked_at"][:19] + " (Новосибирск, UTC+7)")
        for row in obj["rows"]:
            p(
                f"{row['requirement']} · {row['label']} · "
                + ("Обязательное" if row["priority"] == "required" else "Желательное")
            )
            p("Значение: " + row["value"])
            p(row["reason"])
            for src in row["sources"]:
                p(
                    f"Источник: {src['source']} · {src['type']} · {src['date']} · {src['scope']} {src['locator']} · {src['status']}"
                    + (
                        " · заменено при разрешении конфликта"
                        if src["superseded"]
                        else ""
                    )
                )
                if src["confirmation"]:
                    p("Основание: " + src["confirmation"])
                if src["url"]:
                    p("Ссылка: " + src["url"])
            if row["question"]:
                p("Вопрос собственнику: " + row["question"])
        e = obj["economics"]
        p("Расходы — отдельно от технической проверки", h2)
        ce = e["commercial_evidence"]
        p(
            f"Общие источники всех сумм и расчётной площади: {ce['source']} · {ce['type']} · {ce['date']} · {ce['status']} · {ce['scope']}. {ce['confirmation']}"
        )
        for name, v in e["monthly"]:
            p(f"{name}: {v if v is not None else 'неизвестно'} ₽")
        p(
            (
                "Полная месячная нагрузка"
                if e["complete"]
                else "Известный месячный подытог"
            )
            + ": "
            + e["monthly_total"]
            + " ₽"
        )
        p(e["budget_label"])
        if e["missing"]:
            p("Неизвестно / конфликт: " + ", ".join(e["missing"]))
        p(
            "Допустимая базовая аренда: "
            + (
                e["allowed_rent"] + " ₽"
                if e["allowed_rent"] is not None
                else "недостаточно данных"
            )
        )
        for name, v in e["once"]:
            p(f"{name}: {v if v is not None else 'неизвестно'} ₽")
        p(
            ("Разовые затраты" if e["once_complete"] else "Известный разовый подытог")
            + ": "
            + e["once_total"]
            + " ₽"
        )
        p(
            "Депозит учитывается отдельно от месячной нагрузки. Возвратность: "
            + ("подтверждена" if e["deposit_refundable"] else "не подтверждена")
        )
        if e["once_missing"]:
            p("Разовые пропуски: " + ", ".join(e["once_missing"]))
        if e["start_gap"] is not None:
            p("Отклонение от стартового бюджета: " + e["start_gap"] + " ₽")
        p(
            f"Стрессовое допущение +{e['stress_addition']} ₽: {e['stress_total']} ₽/мес. Полнота соответствует базовому сценарию."
        )
        p(
            "Допущения / договорные условия: "
            + (e["assumptions"] or "Дополнительные допущения не заданы")
        )
        if obj["listing_url"]:
            p("Объявление (источник): " + obj["listing_url"])
    p(
        "Результат основан на указанных источниках и условиях. Требует проверки актуальности перед принятием решения. Технический статус не заменяет инженерное или юридическое заключение."
    )

    def footer(c, doc):
        c.setFont("LeaseFIT", 8)
        c.drawString(40, 25, f"LeaseFIT · версия {s['version']} · страница {doc.page}")

    SimpleDocTemplate(
        buf,
        rightMargin=40,
        leftMargin=40,
        topMargin=38,
        bottomMargin=42,
        title="LeaseFIT — клиентский отчёт",
        author=s["agency"],
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
