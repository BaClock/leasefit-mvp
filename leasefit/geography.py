"""City-specific district choices and compatibility with legacy request text."""

import re

NOVOSIBIRSK = "Новосибирск"
ANY_DISTRICT = "*"
DISTRICTS_BY_CITY = {
    "новосибирск": (
        "Дзержинский",
        "Железнодорожный",
        "Заельцовский",
        "Калининский",
        "Кировский",
        "Ленинский",
        "Октябрьский",
        "Первомайский",
        "Советский",
        "Центральный",
    ),
}


def place_key(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def district_key(value):
    return re.sub(r"^(?:район|р-н)\s+|\s+(?:район|р-н)$", "", place_key(value))


def districts_for_city(city):
    return DISTRICTS_BY_CITY.get(place_key(city), ())


def requested_districts(req, available=()):
    registered = districts_for_city(req.city)
    if registered and req.districts is not None:
        return req.districts or None, []
    if not req.geography.strip():
        return None, []
    requested = {
        district_key(part): part.strip()
        for part in re.split(r"[,;/\n]+", req.geography)
        if part.strip()
    }
    known = {district_key(name): name for name in (registered or available)}
    return (
        [name for key, name in known.items() if key in requested],
        [original for key, original in requested.items() if key not in known],
    )


def district_description(req):
    if districts_for_city(req.city) and req.districts is not None:
        return ", ".join(req.districts) or "Любой район"
    return req.geography
