"""Passe un PDF source à Mistral OCR et écrit `<sortie>.md` (marqueurs de page) et `<sortie>.json`.

Même service et même modèle épinglé que le pipeline (`src/services/mistral_ocr_service.py`,
`MISTRAL_OCR_MODEL`). À lancer depuis `mibeko-python/`, venv actif :

    PYTHONPATH=. python data/curation/code-du-travail/ocr_mistral.py \
        data/sources/sgg/codes/congo-code-1975-travail.pdf \
        data/curation/code-du-travail/sgg-1975-mistral-ocr

Les sources vivent dans le dépôt principal (`data/sources/` n'est pas versionné) : depuis un
worktree, passer leur chemin absolu.
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

from src.services.mistral_ocr_service import MistralOcrService  # noqa: E402


async def main(pdf: Path, sortie: Path) -> None:
    service = MistralOcrService()
    markdown, brut = await service.extract(pdf)
    sortie.with_suffix(".md").write_text(markdown, encoding="utf-8")
    sortie.with_suffix(".json").write_text(brut, encoding="utf-8")
    print(f"{len(markdown)} caractères ; modèle {service.model}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
