"""Create a local .env with random secrets; never overwrite an existing file."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
content = (root / ".env.example").read_text()
content = content.replace(
    "replace-with-random-secret-at-least-50-characters", secrets.token_urlsafe(48)
)
content = content.replace(
    "replace-with-random-database-password", secrets.token_urlsafe(32)
)
try:
    fd = os.open(root / ".env", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    print(".env уже существует; сохранён без изменений.")
else:
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print(
        "Создан .env для локального запуска со случайными ключом Django и паролем БД."
    )
