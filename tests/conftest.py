"""Utilitaires partagés à la suite de tests.

`stub_service_modules` neutralise temporairement MinIO/MinerU
(src.services.minio_service / src.services.mineru_service) pour permettre
d'importer `src.api.main` sans backend réel. Contrairement à un simple
`sys.modules[...] = fake` non restauré, ce context manager remet
`sys.modules` dans son état d'origine en sortie de bloc : sans ça, la
pollution (cache process-wide) survit aux fichiers qui l'utilisent et peut
faire récupérer le faux singleton (`mineru_service = object()`, sans
`.backend`) à un import différé lancé plus tard dans la même session pytest
(ex. `src/parsing/batch.py::_mineru_backend_label`).
"""

import sys
import types
from contextlib import contextmanager

_STUBBED_MODULES = [
    ("src.services.minio_service", "minio_service"),
    ("src.services.mineru_service", "mineru_service"),
]


@contextmanager
def stub_service_modules():
    originals = {name: sys.modules.get(name) for name, _ in _STUBBED_MODULES}
    for name, attr in _STUBBED_MODULES:
        fake = types.ModuleType(name)
        setattr(fake, attr, object())
        sys.modules[name] = fake
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
