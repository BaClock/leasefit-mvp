"""Presentation-style client PDF. No live maps, outside requests or attachments."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from .services import money_text

PAGE = (1080, 608)
INK = colors.HexColor("#22324A")
MUTED = colors.HexColor("#64748B")
ACCENT = colors.HexColor("#4F56C8")
GREEN = colors.HexColor("#32BB8F")
PANEL = colors.HexColor("#F3F5FC")
BORDER = colors.HexColor("#DDE3F0")


def render_pdf(s):
    if "LeaseFIT" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("LeaseFIT", settings.PDF_FONT))
        bold = Path(settings.PDF_FONT).with_name(
            Path(settings.PDF_FONT).stem + "-Bold.ttf"
        )
        pdfmetrics.registerFont(
            TTFont("LeaseFITBold", str(bold) if bold.exists() else settings.PDF_FONT)
        )
        pdfmetrics.registerFontFamily(
            "LeaseFIT", normal="LeaseFIT", bold="LeaseFITBold"
        )
    styles = {
        "body": ParagraphStyle(
            "body",
            fontName="LeaseFIT",
            fontSize=9,
            leading=14,
            textColor=INK,
            spaceAfter=7,
            wordWrap="CJK",
        ),
        "small": ParagraphStyle(
            "small",
            fontName="LeaseFIT",
            fontSize=8,
            leading=12,
            textColor=MUTED,
            spaceAfter=5,
            wordWrap="CJK",
        ),
        "h1": ParagraphStyle(
            "h1",
            keepWithNext=True,
            fontName="LeaseFITBold",
            fontSize=26,
            leading=32,
            textColor=GREEN,
            spaceAfter=16,
            wordWrap="CJK",
        ),
        "h2": ParagraphStyle(
            "h2",
            keepWithNext=True,
            fontName="LeaseFITBold",
            fontSize=14,
            leading=20,
            textColor=ACCENT,
            spaceBefore=12,
            spaceAfter=10,
            wordWrap="CJK",
        ),
        "metric": ParagraphStyle(
            "metric",
            fontName="LeaseFITBold",
            fontSize=20,
            leading=27,
            textColor=INK,
            spaceAfter=7,
        ),
    }
    width = PAGE[0] - 252
    story = []

    def para(text, kind="body"):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[kind])

    def p(text, kind="body"):
        story.append(para(text, kind))

    def title(text):
        p(text, "h1")

    def panel(items, col_width=None):
        table = Table([[items]], colWidths=[col_width or width])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                    ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                    ("LEFTPADDING", (0, 0), (-1, -1), 16),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                    ("TOPPADDING", (0, 0), (-1, -1), 14),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                ]
            )
        )
        return table

    def metrics(e):
        labels = [
            "Полная месячная нагрузка"
            if e["complete"]
            else "Известный месячный подытог",
            "Разовые затраты" if e["once_complete"] else "Известный разовый подытог",
            "Допустимая базовая аренда",
        ]
        vals = [e["monthly_total"], e["once_total"], e["allowed_rent"]]
        cells = [
            panel([para(label, "small"), para(money(v), "metric")], (width - 20) / 3)
            for label, v in zip(labels, vals)
        ]
        table = Table([cells], colWidths=[width / 3] * 3)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 12))

    def costs(rows):
        data = [[para(name), para(money(value))] for name, value in rows]
        table = Table(data, colWidths=[width * 0.72, width * 0.28], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 8))

    def money(v):
        return money_text(v) if v is not None else "Недостаточно данных"

    title("Проверка помещений под ваш бизнес")
    p(s["request"], "h2")
    p(f"{s['city']} · {s['business']} · {s['agency']} · {s['author']}")
    p(
        f"Месячный лимит: {money(s['budget'])}. Бюджет на старт: {money(s['start_budget'])}."
    )
    if s["comment"]:
        p("Комментарий риелтора: " + s["comment"])
    p("Короткий список вариантов", "h2")
    cards = []
    for i, obj in enumerate(s["objects"], 1):
        e = obj["economics"]
        cards.append(
            panel(
                [
                    para(f"{i:02d} · {obj['title']}", "h2"),
                    para(obj["label"]),
                    para(obj["decision"]),
                    para(money(e["monthly_total"]) + " / мес.", "metric"),
                    para(e["budget_label"], "small"),
                    para(
                        "Полная нагрузка"
                        if e["complete"]
                        else "Подытог — есть неизвестные платежи",
                        "small",
                    ),
                ],
                (width - 20) / 2,
            )
        )
    if len(cards) % 2:
        cards.append("")
    table = Table(
        [cards[i : i + 2] for i in range(0, len(cards), 2)], colWidths=[width / 2] * 2
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(table)
    p("Профиль и критерии клиента", "h2")
    if s["geography"]:
        p("Районы и ориентиры: " + s["geography"])
    for req in s["requirements"]:
        p(req)
    p(
        "Технический статус и бюджет оцениваются отдельно. Отсеянный вариант не является рекомендацией, условный требует ответов собственника.",
        "small",
    )

    selected = set(s.get("parameters", []))
    for index, obj in enumerate(s["objects"], 1):
        story.append(PageBreak())
        title(f"{index:02d} · {obj['title']}")
        story.append(
            panel(
                [
                    para(obj["label"], "h2"),
                    para(obj["decision"]),
                    para(
                        "Дата проверки: "
                        + obj["checked_at"][:19]
                        + " · Новосибирск (UTC+7)",
                        "small",
                    ),
                ]
            )
        )
        p("Расходы и бюджет", "h2")
        e = obj["economics"]
        metrics(e)
        p(e["budget_label"])
        if e["missing"]:
            p("Не определено / конфликт: " + ", ".join(e["missing"]))
        if e["once_missing"]:
            p("Неизвестные разовые платежи: " + ", ".join(e["once_missing"]))
        p(
            "Депозит отдельно от месячной нагрузки. Возвратность: "
            + ("подтверждена" if e["deposit_refundable"] else "не подтверждена")
            + "."
        )
        if e["start_gap"] is not None:
            p("Отклонение от стартового бюджета: " + money(e["start_gap"]))
        p("Технические требования и причины", "h2")
        for row in obj["rows"]:
            p(
                row["requirement"]
                + " · "
                + row["label"]
                + " · "
                + ("Обязательное" if row["priority"] == "required" else "Желательное"),
                "h2",
            )
            p("Значение: " + row["value"])
            p(row["reason"])
            if row["question"]:
                p("Вопрос собственнику: " + row["question"])
        if "economics:monthly" in selected:
            p("Ежемесячные платежи", "h2")
            costs(e["monthly"])
        if "economics:once" in selected:
            p("Разовые платежи", "h2")
            costs(e["once"])
        if "economics:stress" in selected:
            p("Стрессовый сценарий — допущение", "h2")
            p(
                f"Дополнительные расходы +{money(e['stress_addition'])}/мес. → {money(e['stress_total'])}/мес. Полнота соответствует базовому сценарию."
            )
        p("Источники, допущения и открытые вопросы", "h2")
        ce = e["commercial_evidence"]
        p(
            f"Коммерческие условия: {ce['source']} · {ce['type']} · {ce['date']} · {ce['status']} · {ce['scope']}. {ce['confirmation']}"
        )
        for row in obj["rows"]:
            for src in row["sources"]:
                p(
                    row["name"]
                    + " — "
                    + src["source"]
                    + " · "
                    + src["type"]
                    + " · "
                    + src["date"]
                    + " · "
                    + src["scope"]
                    + " "
                    + src["locator"]
                    + " · "
                    + src["status"]
                    + (
                        " · заменено при разрешении конфликта"
                        if src["superseded"]
                        else ""
                    ),
                    "small",
                )
                if src["confirmation"]:
                    p("Основание: " + src["confirmation"], "small")
                if src["url"]:
                    p("Источник: " + src["url"], "small")
        p(
            "Допущения и договорные условия: "
            + (e["assumptions"] or "Дополнительные допущения не заданы")
        )
        if obj["listing_url"]:
            p("Объявление (источник): " + obj["listing_url"], "small")
    if "future:leads" in selected:
        story.append(PageBreak())
        title("Будущий модуль · карта лидов")
        story.append(
            panel(
                [
                    para("Нет подключенного источника данных", "h2"),
                    para(
                        "Карта лидов, трафик и окружение появятся на следующем этапе. В этой версии геоаналитика не выполняется; показатели спроса и точки на карте не вымышляются."
                    ),
                    Spacer(1, 65),
                    para("Место для будущего картографического раздела", "small"),
                    Spacer(1, 65),
                ]
            )
        )
    p(
        "Результат основан на указанных источниках и условиях. Проверьте актуальность перед решением. Отчёт не заменяет инженерное или юридическое заключение.",
        "small",
    )

    def chrome(c, doc):
        w, h = PAGE
        c.saveState()
        c.setFillColor(colors.white)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        c.setFillColor(PANEL)
        c.rect(0, 0, 202, h, fill=1, stroke=0)
        c.setStrokeColor(BORDER)
        c.line(202, 0, 202, h)
        c.setFillColor(ACCENT)
        c.setFont("LeaseFITBold", 24)
        c.drawString(24, h - 46, "LeaseFIT")
        label = para(s["agency"], "small")
        _, lh = label.wrap(156, 70)
        label.drawOn(c, 24, h - 70 - lh)
        c.setFillColor(MUTED)
        c.setFont("LeaseFITBold", 8)
        c.drawString(24, h - 137, "КЛИЕНТСКИЙ ОТЧЁТ")
        for i, text in enumerate(
            [
                "Общая информация",
                "Профиль бизнеса",
                "Требования и блокеры",
                "Расходы и бюджет",
                "Источники и вопросы",
                "Карта лидов · будущий модуль",
            ]
        ):
            line = para(text, "small")
            _, th = line.wrap(152, 60)
            line.drawOn(c, 26, h - 172 - i * 35 - th)
        c.setFillColor(ACCENT)
        c.roundRect(22, 27, 156, 30, 15, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("LeaseFITBold", 8)
        c.drawString(36, 38, f"Сохранённая версия {s['version']}")
        c.setFillColor(MUTED)
        c.setFont("LeaseFIT", 8)
        c.drawString(226, h - 28, "АНАЛИЗ ПОМЕЩЕНИЙ / " + s["city"][:60])
        c.drawRightString(
            w - 28, 19, f"LeaseFIT · версия {s['version']} · страница {doc.page}"
        )
        c.drawString(
            226,
            19,
            s["issued_at"][:19] + " · Новосибирск (UTC+7) · " + s["rules_version"],
        )
        c.restoreState()

    buf = BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=PAGE,
        leftMargin=226,
        rightMargin=26,
        topMargin=53,
        bottomMargin=38,
        title="LeaseFIT — клиентский отчёт",
        author=s["agency"],
    ).build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()
