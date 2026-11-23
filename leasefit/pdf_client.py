"""Short LeaseFIT report designed around the rental decision."""

from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .outcomes import money_text
from .pdf import register_fonts
from .report_content import CLIENT_SECTIONS, public_text

PAGE = (960, 685)
MARGIN = 42
WIDTH = PAGE[0] - 2 * MARGIN
INK = colors.HexColor("#16243C")
MUTED = colors.HexColor("#4C5F76")
ACCENT = colors.HexColor("#2854BA")
LINE = colors.HexColor("#D7E1EE")
PALE = colors.HexColor("#F3F7FC")
RED = colors.HexColor("#A52B3E")
GOLD = colors.HexColor("#795319")
GREEN = colors.HexColor("#176852")

STYLES = {}
for name, size, leading, color, bold in (
    ("body", 11, 15, INK, False),
    ("small", 9.5, 13, MUTED, False),
    ("cell", 9.5, 12, INK, False),
    ("label", 9.5, 13, MUTED, True),
    ("strong", 11, 15, INK, True),
    ("title", 24, 29, INK, True),
    ("heading", 15, 20, ACCENT, True),
    ("red", 10, 14, RED, True),
    ("gold", 10, 14, GOLD, True),
    ("green", 10, 14, GREEN, True),
):
    STYLES[name] = ParagraphStyle(
        name, fontName="LeaseFITBold" if bold else "LeaseFIT",
        fontSize=size, leading=leading, textColor=color, splitLongWords=True,
    )


def para(text, style="body"):
    return Paragraph(escape(public_text(str(text))).replace("\n", "<br/>"), STYLES[style])


def money(value):
    return "Нет данных" if value is None else money_text(value)


def tone(obj):
    return "red" if obj["blocked"] else "green" if obj["recommended"] else "gold"


def table(rows, widths, *, repeat=1, header=True, padding=6):
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("LINEBELOW", (0, 0), (-1, -1), .45, LINE),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), PALE))
    return Table(rows, colWidths=widths, repeatRows=repeat, hAlign="LEFT", style=style)


def heading(text):
    return [para(text, "heading"), Spacer(1, 6)]


def comparison(objects):
    rows = [[para(label, "label") for label in (
        "Помещение", "Месячные расходы", "Отклонение от бюджета",
        "Техническая проверка", "Главный риск", "Итог",
    )]]
    for i, obj in enumerate(objects, 1):
        monthly = obj["monthly"]
        rows.append([
            [para(f"{i}. {obj['title']}", "strong"), para(f"{obj['district']} · {obj['area']}", "small")],
            [
                para(money(monthly["amount"]), "strong"),
                *([para("Полная сумма требует уточнения", "small")] if not monthly["complete"] else []),
            ],
            para(obj["difference"], "red" if monthly["state"] == "over" else "cell"),
            para(obj["technical"], "cell"),
            para(obj["limits"][0]["title"] if obj["limits"] else "Существенных ограничений не выявлено", "cell"),
            para(obj["comparison_status"], tone(obj)),
        ])
    return table(rows, [217, 118, 104, 121, 147, WIDTH - 707], padding=4)


def object_card(obj, number, width):
    fields = [
        para(f"{number}. {obj['title']}", "heading"),
        para(f"{obj['district']} · {obj['area']} · {obj['monthly_label']} — {money(obj['monthly']['amount'])}", "small"),
        Spacer(1, 3),
        para(obj["verdict"], tone(obj)),
    ]
    if obj["limits"]:
        fields.extend([
            Spacer(1, 3), para("Главные ограничения", "label"),
            *[para("• " + issue["title"], "cell") for issue in obj["limits"]],
        ])
    if obj["benefits"]:
        fields.extend([
            Spacer(1, 3), para("Подтверждённые преимущества", "label"),
            *[para("• " + benefit, "cell") for benefit in obj["benefits"]],
        ])
    if obj["limits"]:
        fields.extend([
            Spacer(1, 3), para("Что сделать", "label"),
            *[para("• " + action, "cell") for action in dict.fromkeys(
                issue["action"] for issue in obj["limits"][:2]
            )],
        ])
    return table([[fields]], [width], repeat=0, padding=6)


def cards(objects):
    gap = 12
    width = (WIDTH - gap) / 2
    story = []
    for start in range(0, len(objects), 2):
        pair = [object_card(obj, i, width) for i, obj in enumerate(objects[start:start + 2], start + 1)]
        if len(pair) == 1:
            pair.append("")
        story.append(Table(
            [[pair[0], "", pair[1]]], colWidths=[width, gap, width], hAlign="LEFT",
            style=[
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ],
        ))
        story.append(Spacer(1, gap))
    return story


def financial_table(objects, labels_and_values, width, *, label_header="Статья расходов"):
    label_width = 118 if width < WIDTH else 180
    rows = [[para(label_header, "label")] + [
        para(f"Вариант {i}", "label") for i in range(1, len(objects) + 1)
    ]]
    for label, values in labels_and_values:
        rows.append([para(label, "label"), *[para(value, "cell") for value in values]])
    return table(rows, [label_width] + [(width - label_width) / len(objects)] * len(objects), padding=3)


def cost_lines(objects, key):
    columns = [dict(obj[key]) for obj in objects]
    labels = dict.fromkeys(label for column in columns for label in column)
    return [(label, [money(column.get(label)) for column in columns]) for label in labels]


def finances(objects, start_budget):
    gap = 12
    width = (WIDTH - gap) / 2
    monthly_fields = heading("Расходы в месяц")
    lines = cost_lines(objects, "monthly_rows")
    lines.extend([
        ("Нагрузка за месяц", [
            money(obj["monthly"]["amount"]) + (
                " · подытог" if obj["monthly"]["amount"] is not None
                and not obj["monthly"]["complete"] else ""
            ) for obj in objects
        ]),
        ("Отклонение от бюджета", [
            f"{obj['monthly']['sign']}{money(obj['monthly']['difference'])}"
            if obj["monthly"]["difference"] is not None else "Уточнить"
            for obj in objects
        ]),
    ])
    monthly_fields.extend([financial_table(objects, lines, width), Spacer(1, 6)])
    for i, obj in enumerate(objects, 1):
        if obj["monthly_missing"]:
            missing = ", ".join(obj["monthly_missing"])
            monthly_fields.append(para(f"Вариант {i}. Уточнить: {missing}.", "small"))
        elif not obj["monthly"]["confirmed"]:
            monthly_fields.append(para(f"Вариант {i}. {obj['monthly']['evidence']}", "small"))

    opening_fields = heading("Деньги на открытие")
    opening_fields.extend([
        para(f"Лимит на открытие — {money(start_budget)}. Депозит учитывается только в расходах на открытие.", "small"),
        Spacer(1, 5),
    ])
    if any(obj["opening_total"] is not None for obj in objects):
        lines = cost_lines(objects, "opening_rows")
        lines.append(("Сумма на открытие", [
            money(obj["opening_total"]) + (
                " · подытог" if obj["opening_total"] is not None
                and obj["opening_missing"] else ""
            ) for obj in objects
        ]))
        opening_fields.extend([financial_table(objects, lines, width), Spacer(1, 6)])
        for i, obj in enumerate(objects, 1):
            if obj["opening_missing"]:
                missing = ", ".join(obj["opening_missing"]).lower()
                opening_fields.append(para(f"Вариант {i}. Неполная смета. Уточнить: {missing}.", "small"))
            elif obj["opening_comparison"]:
                opening_fields.append(para(f"Вариант {i}. {obj['opening_comparison']}.", "small"))
    else:
        opening_fields.append(para("Суммы на открытие пока не указаны."))

    story = [Table(
        [[monthly_fields, "", opening_fields]], colWidths=[width, gap, width], hAlign="LEFT",
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )]
    fields = []
    if any(obj["break_even"] is not None for obj in objects):
        volumes = []
        for obj in objects:
            text = f"{obj['break_even']} в месяц" if obj["break_even"] is not None else "Нет данных"
            if obj["daily_break_even"] is not None:
                text += f" · {obj['daily_break_even']} в день ({obj['working_days']} раб. дней в месяц)"
            volumes.append(text)
        fields.append(("Операций для безубыточности", volumes))
    for label, rate in (
        ("Базовый сценарий", "0"), ("Снижение на 10%", "10"), ("Снижение на 20%", "20"),
    ):
        amounts = [
            next((row["profit"] for row in obj["scenarios"] if row["percent"] == rate), None)
            for obj in objects
        ]
        if any(amount is not None for amount in amounts):
            fields.append((label, [money(amount) for amount in amounts]))
    if fields:
        basis = "вознаграждения за операцию" if objects[0]["pickup"] else "среднего чека"
        story.append(KeepTogether([
            Spacer(1, 10), *heading("Безубыточность и сценарии"),
            financial_table(objects, fields, WIDTH, label_header="Показатель"),
            Spacer(1, 5), para(
                f"Расчёт зависит от {basis}, маржинальности и графика. "
                "Сценарии показывают расчётный финансовый результат за месяц.", "small",
            ),
        ]))
    return story


def render_client_pdf(snapshot):
    register_fonts()
    content = snapshot["client_content"]
    objects = content["objects"]
    summary = content["summary"]
    agency = snapshot["agency"]
    agency_footer = public_text(agency)
    issued = datetime.fromisoformat(snapshot["issued_at"]).strftime("%d.%m.%Y") if snapshot.get("issued_at") else ""
    story = [
        para(CLIENT_SECTIONS[0], "title"), Spacer(1, 6),
        para(snapshot["request"], "heading"),
        para(" · ".join(filter(None, (agency, snapshot["business"], snapshot["city"], snapshot["geography"], issued))), "small"),
        Spacer(1, 8),
        table([[
            [
                para("Главный вывод", "label"),
                *[para(item, "heading") for item in summary["headlines"]],
                para(summary["recommendation"]),
            ],
            [
                para("Лимит на помещение в месяц", "label"), para(money(snapshot["budget"]), "heading"),
                para("Лимит на открытие", "label"), para(money(snapshot["start_budget"]), "strong"),
            ],
        ]], [WIDTH - 225, 225], repeat=0, padding=6),
        Spacer(1, 7), *heading("Сравнение помещений"), comparison(objects), Spacer(1, 7),
    ]
    groups = []
    for key, label in (
        ("recommended", "Рассмотреть в первую очередь"),
        ("conditional", "Вернуться после уточнений"),
        ("excluded", "Исключить при текущих условиях"),
    ):
        if summary[key]:
            groups.append(f"{label} — {', '.join(summary[key])}")
    if groups:
        story.append(para(" · ".join(groups), "strong"))
    if summary["actions"]:
        action = summary["actions"][0]
        story.append(para(f"Первый шаг · {action['text']}"))
    story.extend([PageBreak(), para(CLIENT_SECTIONS[1], "title"), Spacer(1, 12), *cards(objects)])
    story.extend([PageBreak(), para(CLIENT_SECTIONS[2], "title"), Spacer(1, 10), *finances(objects, snapshot["start_budget"])])
    story.extend([PageBreak(), para(CLIENT_SECTIONS[3], "title"), Spacer(1, 12)])
    if summary["actions"]:
        for i, action in enumerate(summary["actions"], 1):
            variants = ", ".join(action["objects"])
            label = "вариант" if len(action["objects"]) == 1 else "варианты"
            story.extend([
                para(f"{i}. {action['text']} ({label} {variants})"),
                Spacer(1, 6),
            ])
    else:
        story.append(para("По результатам проверки дополнительных уточнений нет."))
    story.extend([
        Spacer(1, 12), *heading("Заключение риелтора"),
        para(snapshot["broker_conclusion"]["label"], "strong"),
        para("Это мнение риелтора. Итоги проверки для каждого помещения приведены в сравнении.", "small"),
    ])
    if content["comment"]:
        story.extend([Spacer(1, 6), para(content["comment"])])

    def chrome(canvas, doc):
        width, height = PAGE
        canvas.setTitle(snapshot["format_label"])
        canvas.setFillColor(ACCENT)
        canvas.roundRect(MARGIN, height - 43, 26, 22, 5, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("LeaseFITBold", 11)
        canvas.drawCentredString(MARGIN + 13, height - 36, "LF")
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN + 34, height - 36, "LeaseFIT · Анализ коммерческого помещения")
        canvas.setFillColor(MUTED)
        canvas.setFont("LeaseFIT", 9)
        canvas.drawRightString(width - MARGIN, height - 36, snapshot["format_label"])
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, 34, width - MARGIN, 34)
        canvas.drawString(MARGIN, 19, agency_footer[:100])
        canvas.drawCentredString(width / 2, 19, issued)
        canvas.drawRightString(width - MARGIN, 19, str(doc.page))

    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream, pagesize=PAGE, leftMargin=MARGIN - 6, rightMargin=MARGIN - 6,
        topMargin=54, bottomMargin=43, allowSplitting=1,
    )
    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return stream.getvalue()
