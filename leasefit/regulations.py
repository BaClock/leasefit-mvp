"""Preliminary document checklist. Legal applicability is distinct from fit score."""

from dataclasses import asdict, dataclass
from datetime import date

from django.utils import timezone

CHECKED_ON = "2026-11-19"
REGISTRY_VERSION = "2026.11.19"
STATUS = {
    "positive": "Предварительно соответствует",
    "negative": "Выявлено несоответствие",
    "pending": "Требует подтверждения",
    "na": "Не применяется",
}


@dataclass(frozen=True)
class Document:
    title: str
    revision: str
    url: str
    start: str
    end: str = ""
    legal_status: str = "Обязательное требование в пределах применимости"


SANPIN = Document(
    "СанПиН 2.3/2.4.4282-26",
    "Постановление от 02.06.2026 № 18",
    "https://publication.pravo.gov.ru/document/0001202606020084",
    "2026-09-01",
    "2032-09-01",
)
FOOD_SAFETY = Document(
    "ТР ТС 021/2011",
    "Решение КТС от 09.12.2011 № 880, ст. 12 и 14",
    "https://eec.eaeunion.org/upload/medialibrary/6ad/TR-TS-PishevayaProd.pdf",
    "2013-07-01",
)
OPERATING_SANITATION = Document(
    "СП 2.1.4284-26",
    "Постановление от 02.06.2026 № 20",
    "https://publication.pravo.gov.ru/document/0001202606020088",
    "2026-09-01",
    "2032-09-01",
)
FIRE = Document(
    "Федеральный закон № 123-ФЗ",
    "Федеральный закон от 22.07.2008, ст. 89",
    "https://moscow.mchs.gov.ru/deyatelnost/rabota-s-obrashcheniyami-grazhdan/chasto-zadavaemye-voprosy/voprosy-nadzornoy-deyatelnosti-i-profilakticheskoy-raboty/trebovaniya-pozharnoy-bezopasnosti-k-evakuacionnym-putyam-evakuacionnym-i-avariynym-vyhodam",
    "2009-05-01",
)


@dataclass(frozen=True)
class Rule:
    code: str
    title: str
    document: Document
    section: str
    applies: str
    characteristics: str
    evidence: str
    method: str
    answer: str = ""


RULES = (
    Rule(
        "san-water",
        "Холодное и горячее водоснабжение",
        SANPIN,
        "п. 18",
        "Работа предприятия общепита.",
        "Исправность систем, горячая и холодная вода.",
        "Попросите схему и сведения о подключении или инженерный акт.",
        "Сверить схему и осмотр с технологией.",
    ),
    Rule(
        "san-sewer",
        "Водоотведение",
        SANPIN,
        "п. 18",
        "Работа предприятия общепита.",
        "Исправность отвода стоков, отсутствие риска загрязнения.",
        "Попросите схему водоотведения и сведения о точках подключения.",
        "Сверить подключение и расположение коммуникаций с планом.",
    ),
    Rule(
        "san-air",
        "Вентиляция",
        SANPIN,
        "п. 18",
        "Работа предприятия общепита.",
        "Состояние и пригодность вентиляции для планируемой работы.",
        "Попросите паспорт вентиляции и схему притока и вытяжки.",
        "Сопоставить инженерную схему с процессами и расположением источников загрязнения.",
    ),
    Rule(
        "san-control",
        "Технологические процессы и производственный контроль",
        SANPIN,
        "п. 4",
        "Предприятие организует общественное питание.",
        "Перечень операций и точки производственного контроля.",
        "Запросите схему процессов и программу контроля на принципах ХАССП.",
        "Сопоставить программу контроля с процессами и планом объекта.",
    ),
    Rule(
        "san-flow",
        "Планировка и поточность",
        SANPIN,
        "п. 8",
        "В помещении планируется приготовление пищи или напитков.",
        "Потоки сырья, готовой продукции, посуды, посетителей и персонала.",
        "Попросите план с расстановкой зон и потоками.",
        "Проверить потоки и применимость предусмотренных правилом условий.",
        "preparation",
    ),
    Rule(
        "san-storage",
        "Хранение продукции",
        SANPIN,
        "п. 28",
        "Планируется хранение сырья или продукции.",
        "Зоны хранения и температурно-влажностные условия.",
        "Запросите план хранения и перечень холодильного оборудования.",
        "Сопоставить условия хранения с перечнем продуктов.",
        "food_storage",
    ),
    Rule(
        "san-intake",
        "Приёмка продукции",
        SANPIN,
        "п. 5",
        "Планируется приёмка продукции.",
        "Организация приёмки, маркировка и товаросопроводительные сведения.",
        "Попросите описать приёмку и показать образец сопровождающих документов.",
        "Проверить, позволяет ли планируемый процесс сверять документы при приёмке.",
        "food_delivery",
    ),
    Rule(
        "san-finish",
        "Санитарно-бытовые помещения и отделка",
        SANPIN,
        "п. 19",
        "Используются производственные и санитарно-бытовые помещения общепита.",
        "Внутренняя отделка, состояние поверхностей и возможность влажной уборки.",
        "Запросите план и фотографии отделки или акт осмотра.",
        "Проверить состояние и пригодность поверхностей для уборки.",
    ),
    Rule(
        "san-waste",
        "Обращение с отходами",
        SANPIN,
        "п. 20",
        "При работе предприятия образуются отходы.",
        "Сбор и удаление отходов, обслуживание территории.",
        "Попросите схему маршрута отходов и сведения об их вывозе.",
        "Сверить схему обращения с отходами по применимым правилам.",
    ),
    Rule(
        "sp-water",
        "Тип подключения воды и водоотведения",
        OPERATING_SANITATION,
        "п. 4",
        "Подтверждается тип системы используемого помещения. При отсутствии централизованных систем проверяется автономное решение.",
        "Подключение холодной и горячей воды, водоотведение.",
        "Запросите схему подключения или проект автономной системы и отвода стоков.",
        "По схеме уточнить тип подключения и проверить применимые условия.",
    ),
    Rule(
        "tr-water",
        "Вода для производства пищевой продукции",
        FOOD_SAFETY,
        "ст. 12, ч. 1 и ч. 2 п. 1",
        "Планируется производство пищевой продукции.",
        "Достаточность воды, питьевое качество при непосредственном контакте.",
        "Попросите документы о качестве воды и технологической потребности.",
        "Сверить вид использования воды с документами о качестве и производственным процессом.",
        "preparation",
    ),
    Rule(
        "fire-exits",
        "Эвакуационные пути и выходы",
        FIRE,
        "ст. 89, ч. 3",
        "Необходимо оценить проектную эвакуацию посетителей и персонала.",
        "Фактические пути, выходы, этаж и назначение помещения.",
        "Запросите поэтажный план с путями выхода и заключение профильного специалиста.",
        "Проверить направление выходов, применимый пожарный класс и проект.",
    ),
)

REFERENCES = (
    (
        "ГОСТ 30389-2013",
        "https://protect.gost.ru/gost?search=30389-2013",
        "Действующий добровольный стандарт классификации. Учтена карточка поправки от 15.05.2022.",
    ),
    (
        "ГОСТ 31984-2012",
        "https://protect.gost.ru/gost/details/b16670a8-8b82-4127-b58b-28ecaf9c5311",
        "Действующий добровольный стандарт общих требований к услугам.",
    ),
    (
        "СП 1.13130.2020 и строительные нормы проекта",
        "https://mchs.gov.ru/dokumenty/inye-normativnye-pravovye-akty/prochie-normativnye-pravovye-akty/svody-pravil",
        "Уточнить редакцию, пожарный класс, строительное решение и применимые пункты специалистом.",
    ),
)


def regulatory_checklist(req, link=None, *, on_date=None, with_notes=False):
    if not req.business_format.startswith("food."):
        return []
    day = (on_date or timezone.localdate()).isoformat()
    answers = req.business_answers
    reviews = link.regulatory_reviews if link else {}
    results = []
    for rule in RULES:
        if day < rule.document.start or (
            rule.document.end and day >= rule.document.end
        ):
            continue
        status = "pending"
        applicability = rule.applies
        answer = answers.get(rule.answer) if rule.answer else "yes"
        if answer == "no":
            status = "na"
            applicability += " По ответам клиента этот процесс не планируется."
        elif answer is None:
            applicability += " Применимость нужно уточнить у клиента."
        review = reviews.get(rule.code, {})
        current = (
            link
            and review.get("registry") == REGISTRY_VERSION
            and review.get("profile") == req.version
            and review.get("premises") == link.premises.version
        )
        if current and status != "na":
            status = review["status"]
        results.append(
            {
                **asdict(rule),
                "status": status,
                "label": STATUS[status],
                "applicability": applicability,
                "registry": REGISTRY_VERSION,
                "checked_on": CHECKED_ON,
                "note": review.get("note", "") if with_notes and current else "",
                "reviewable": answer != "no",
            }
        )
    return results
