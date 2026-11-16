"""Read-only manifest for restart/restore verification.
Run: python manage.py shell --no-imports < scripts/storage_manifest.py
"""

import hashlib
import json
from leasefit.models import (
    ClientRequest,
    Premises,
    Observation,
    Evaluation,
    Report,
    Attachment,
)

manifest = {}
for model in [ClientRequest, Premises, Observation, Evaluation]:
    records = list(model.objects.order_by("id").values())
    payload = json.dumps(
        records, sort_keys=True, ensure_ascii=False, default=str
    ).encode()
    manifest[model.__name__] = {
        "count": len(records),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
for model, field in [(Report, "pdf"), (Attachment, "data")]:
    manifest[model.__name__] = [
        {"id": obj.pk, "sha256": hashlib.sha256(bytes(getattr(obj, field))).hexdigest()}
        for obj in model.objects.order_by("id")
    ]
print(json.dumps(manifest, sort_keys=True, indent=2))
