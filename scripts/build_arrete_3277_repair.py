#!/usr/bin/env python3
"""Construit la cible Classe 2 de mibeko-dashboard#56 depuis le Markdown JO.

Ce script ne contacte aucun service et n'écrit jamais en base. Il fige, dans
le dépôt Laravel, l'artefact JSON que l'applicateur de production transmettra
au canal atomique ``replace-extraction`` après retrait public provisoire.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.extractor.parser import LegalDocumentParser  # noqa: E402


MONOREPO_ROOT = REPO_ROOT.parent
SOURCE_MARKDOWN = REPO_ROOT / "data/pipeline/md/sgg-jo/congo-jo-2025-38.md"
SOURCE_PDF = REPO_ROOT / "data/sources/sgg/JO/congo-jo-2025-38.pdf"
OUTPUT = (
    MONOREPO_ROOT
    / "mibeko-tableau-de-bord/storage/app/corrections/2026-08-18-reconstruire-arrete-3277.json"
)

DOCUMENT_ID = "d78c6677-d5ac-461a-b98c-a0161d28c69c"
SOURCE_PDF_SHA256 = "b6904b2283724d450ce495bfb73276cb454f8c8d0943fd706876b482f3fdb305"
JOURNAL_PAGE_OFFSET = 1264

CHAPTER_TITLES = {
    "I": "OBJET ET DEFINITIONS",
    "II": "NATURE ET ZONE DE COUVERTURE DU RESEAU",
    "III": "CARACTERISTIQUES DU RESEAU, DES EQUIPEMENTS ET DES SERVICES",
    "IV": "MODE D’ACCES AU RESEAU, CONDITIONS DE PERMANENCE, DE DISPONIBILITE ET DE QUALITE - UTILISATION DES DOMAINES PUBLIC ET PRIVE",
    "V": "HOMOLOGATION DES EQUIPEMENTS",
    "VI": "INTERCONNEXION DES RESEAUX ET PARTAGE DES INFRASTRUCTURES",
    "VII": "CONCURRENCE",
    "VIII": "INTERVENTION, VISITE ET CONTROLE DES INSTALLATIONS",
    "IX": "RESSOURCES RARES",
    "X": "DROITS, TAXES ET REDEVANCES",
    "XI": "CONDITIONS D’EXPLOITATION COMMERCIALE",
    "XII": "RELATIONS AVEC LES CONSOMMATEURS",
    "XIII": "MESURES A PRENDRE PAR L’AUTORITE DE REGULATION",
    "XIV": "OBLIGATIONS DE L’OPERATEUR",
    "XV": "DUREE, CONDITIONS DE RENOUVELLEMENT ET DE CESSATION DES ACTIVITES",
    "XVI": "SANCTIONS",
    "XVII": "DISPOSITIONS PARTICULIERES",
    "XVIII": "DISPOSITIONS FINALES",
}

CHAPTER_ARTICLES = {
    "I": (1, 2),
    "II": (3, 4),
    "III": (5, 7),
    "IV": (8, 12),
    "V": (13, 14),
    "VI": (15, 20),
    "VII": (21, 21),
    "VIII": (22, 22),
    "IX": (23, 24),
    "X": (25, 27),
    "XI": (28, 28),
    "XII": (29, 37),
    "XIII": (38, 38),
    "XIV": (39, 46),
    "XV": (47, 49),
    "XVI": (50, 50),
    "XVII": (51, 53),
    "XVIII": (54, 57),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def walk(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from walk(node.get("children", []))


def normalized_roman(value: str) -> str:
    # Le PDF imprime bien II ; la couche texte a reconnu le second I comme un l.
    return "II" if value == "Il" else value


def clean_content(value: str) -> str:
    """Retire seulement les artefacts d'extraction prouvés par le fac-similé."""

    # Coupure typographique de fin de ligne : intercon-\nnexion -> interconnexion.
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    # Deux confusions OCR contrôlées sur le rendu du PDF officiel.
    value = value.replace("télécomrnunications", "télécommunications")
    value = value.replace("lmpfondo", "Impfondo")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    value = "\n".join(line for line in lines if line).strip()
    # Le troisième item de l'article 49 est visuellement continu ; le point
    # rond injecté avant « avis motivé » provient de la couche texte.
    return value.replace(
        "télécommunications, après\n•\navis motivé",
        "télécommunications, après\navis motivé",
    )


def locator(node: dict[str, Any], fallback_page: int) -> dict[str, int]:
    page = int(node.get("page") or fallback_page)
    page_end = int(node.get("page_end") or page)
    result = {"page": page, "journal_page": page + JOURNAL_PAGE_OFFSET}
    if page_end != page:
        result["page_end"] = page_end
        result["journal_page_end"] = page_end + JOURNAL_PAGE_OFFSET
    return result


def extract_segments(markdown: str) -> tuple[str, str]:
    title_start = markdown.index("Arrêté n° 3277 du 28 août 2025 portant", 4000)
    main_start = markdown.index(
        "Le ministre des postes, des télécommunications\n et de l’économie numérique,",
        title_start,
    )
    annex_start = markdown.index("CAHIER DES CHARGES\nRELATIF A LA LICENCE", main_start)
    next_act = markdown.index("Arrêté n° 3278 du 28 août 2025 portant", annex_start)
    return markdown[main_start:annex_start], markdown[annex_start:next_act]


def parse_with_first_page(segment: str, page: int) -> list[dict[str, Any]]:
    return LegalDocumentParser(
        text_content=f"[[MIBEKO_PAGE:{page}]]\n{segment}"
    ).parse_hierarchy()


def build_target(markdown: str) -> dict[str, Any]:
    main_segment, annex_segment = extract_segments(markdown)
    main_roots = parse_with_first_page(main_segment, 34)
    annex_roots = parse_with_first_page(annex_segment, 35)

    main_preamble = next(node for node in main_roots if node["type"] == "PREAMBULE")
    main_signature = next(node for node in main_roots if node["type"] == "SIGNATURE")
    main_articles = [node for node in main_roots if node["type"] == "ARTICLE"]
    if [node["number"] for node in main_articles] != [
        "premier", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"
    ]:
        raise RuntimeError("La numérotation du dispositif principal a changé.")

    annex_chapters = [node for node in annex_roots if node["type"] == "CHAPITRE"]
    actual_chapters = [normalized_roman(node["number"]) for node in annex_chapters]
    if actual_chapters != list(CHAPTER_TITLES):
        raise RuntimeError(f"Les chapitres de l'annexe ont changé : {actual_chapters}")

    articles: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    order = 0

    def add_article(
        number: str,
        parent: str | None,
        parsed: dict[str, Any],
        fallback_page: int,
        content: str | None = None,
    ) -> None:
        nonlocal order
        articles.append({
            "number": number,
            "parent": parent,
            "order": order,
            "content": clean_content(parsed["content"] if content is None else content),
            "source_locator": locator(parsed, fallback_page),
        })
        order += 1

    add_article("PREAMBULE", None, main_preamble, 34)
    for article in main_articles:
        add_article(article["number"], None, article, 34)
    add_article("SIGNATURE", None, main_signature, 35)

    annex_key = "annexe_cahier_des_charges_2g"
    nodes.append({
        "key": annex_key,
        "parent": None,
        "type": "ANNEXE",
        "number": None,
        "title": (
            "CAHIER DES CHARGES RELATIF A LA LICENCE D’ETABLISSEMENT ET "
            "D’EXPLOITATION D’UN RESEAU MOBILE DE DEUXIEME GENERATION (2G) "
            "ACCORDEE A LA SOCIETE CONGO TELECOM"
        ),
        "order": order,
    })
    order += 1

    annex_signature_content: str | None = None
    annex_signature_page = 46
    annex_numbers: list[int] = []
    for chapter in annex_chapters:
        roman = normalized_roman(chapter["number"])
        chapter_key = f"annexe_chapitre_{roman.lower()}"
        nodes.append({
            "key": chapter_key,
            "parent": annex_key,
            "type": "CHAPITRE",
            "number": roman,
            "title": CHAPTER_TITLES[roman],
            "order": order,
        })
        order += 1

        chapter_articles = [node for node in chapter["children"] if node["type"] == "ARTICLE"]
        expected_start, expected_end = CHAPTER_ARTICLES[roman]
        actual_numbers = [1 if node["number"] == "premier" else int(node["number"]) for node in chapter_articles]
        if actual_numbers != list(range(expected_start, expected_end + 1)):
            raise RuntimeError(
                f"Numérotation inattendue au chapitre {roman} : {actual_numbers}"
            )

        for parsed, numeric_number in zip(chapter_articles, actual_numbers, strict=True):
            content = parsed["content"]
            if numeric_number == 57:
                signature_marker = "\nFait à Brazzaville, le\n"
                if signature_marker not in content:
                    raise RuntimeError("La signature finale de l'annexe n'a pas été retrouvée.")
                content, signature_tail = content.split(signature_marker, 1)
                annex_signature_content = f"Fait à Brazzaville, le\n{signature_tail}"

            target_number = (
                f"{'premier' if numeric_number == 1 else numeric_number}_doublon_1"
                if numeric_number <= 12
                else str(numeric_number)
            )
            add_article(target_number, chapter_key, parsed, 35, content=content)
            annex_numbers.append(numeric_number)

    if annex_numbers != list(range(1, 58)):
        raise RuntimeError(f"L'annexe n'est pas complète : {annex_numbers}")
    if annex_signature_content is None:
        raise RuntimeError("La signature finale de l'annexe est absente.")

    signature_stub = {"content": annex_signature_content, "page": annex_signature_page}
    add_article("SIGNATURE_doublon_1", annex_key, signature_stub, annex_signature_page)

    target = {
        "schema_version": 1,
        "document_id": DOCUMENT_ID,
        "source_pdf": {
            "filename": SOURCE_PDF.name,
            "sha256": SOURCE_PDF_SHA256,
            "size": SOURCE_PDF.stat().st_size,
        },
        "nodes": nodes,
        "articles": articles,
    }
    validate_target(target)
    return target


def validate_target(target: dict[str, Any]) -> None:
    if len(target["nodes"]) != 19 or len(target["articles"]) != 72:
        raise RuntimeError("La cible doit contenir 19 divisions et 72 unités de contenu.")

    orders = [node["order"] for node in target["nodes"]]
    orders += [article["order"] for article in target["articles"]]
    if sorted(orders) != list(range(91)):
        raise RuntimeError("Les ordres globaux de la cible ne sont pas continus et uniques.")

    numbers = [article["number"] for article in target["articles"]]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("Les numéros techniques d'articles ne sont pas uniques.")

    full_text = "\n".join(article["content"] for article in target["articles"])
    forbidden = [
        "N° 38-2025",
        "Du jeudi 18 septembre 2025",
        "Arrêté n° 3278",
        "télécomrnunications",
    ]
    contaminants = [value for value in forbidden if value in full_text]
    if contaminants:
        raise RuntimeError(f"Contaminants encore présents : {contaminants}")
    if re.search(r"\w-\s*\n\s*\w", full_text):
        raise RuntimeError("Une césure typographique de fin de ligne subsiste.")

    for article in target["articles"]:
        page = article["source_locator"].get("page")
        page_end = article["source_locator"].get("page_end", page)
        if not 34 <= page <= page_end <= 46:
            raise RuntimeError(
                f"Repère hors acte pour {article['number']} : {article['source_locator']}"
            )


def build_plan() -> dict[str, Any]:
    if sha256(SOURCE_PDF) != SOURCE_PDF_SHA256:
        raise RuntimeError("Le PDF local ne correspond plus à l'empreinte officielle attendue.")
    markdown = SOURCE_MARKDOWN.read_text(encoding="utf-8")
    target = build_target(markdown)

    return {
        "operation": "retrait_et_reconstruction_arrete_3277",
        "classe": 2,
        "issue": "https://github.com/benaja-bendo/mibeko-dashboard/issues/56",
        "document_id": DOCUMENT_ID,
        "document_slug": "arrete-n-3277-du-28-aout-2025-portant",
        "document_title": (
            "Arrêté n° 3277 du 28 août 2025 portant attribution d'une licence "
            "d'établissement et d'exploitation d'un réseau mobile de 2e génération "
            "ouvert au public à la société Congo Télécom S.A."
        ),
        "decision": (
            "Retirer provisoirement le document du catalogue en le plaçant en review, "
            "remplacer atomiquement son extraction depuis le PDF officiel, puis laisser "
            "la republication à une validation humaine distincte."
        ),
        "before_measured_at": "2026-08-18",
        "before": {
            "curation_status": "published",
            "live_nodes": 15,
            "live_articles": 62,
            "authority": "Président de la République",
            "unresolved_flags": {"info": 1},
        },
        "source": {
            "url": "https://www.sgg.cg/JO/2025/congo-jo-2025-38.pdf",
            "filename": SOURCE_PDF.name,
            "size": SOURCE_PDF.stat().st_size,
            "sha256": SOURCE_PDF_SHA256,
            "markdown_path": str(SOURCE_MARKDOWN.relative_to(REPO_ROOT)),
            "markdown_sha256": sha256(SOURCE_MARKDOWN),
            "pdf_pages": [34, 46],
            "journal_pages": [1298, 1310],
            "method": "Rendu Poppler et lecture visuelle, extraction structurée depuis le Markdown MinerU canonique.",
        },
        "metadata_patch": {
            "expected_authority": "Président de la République",
            "authority": "Ministre des postes, des télécommunications et de l’économie numérique",
            "marker": "reconstruction_controlee_2026_08_18",
        },
        "expected_after_repair": {
            "curation_status": "review",
            "live_nodes": 19,
            "live_articles": 72,
            "numbered_main_articles": 12,
            "numbered_annex_articles": 57,
            "main_signatures": 1,
            "annex_signatures": 1,
            "missing_source_locators": 0,
            "physical_deletes": 0,
            "minio_writes": 0,
        },
        "remaining_reservations": {
            "legal_status": "Le statut juridique 'vigueur' n'est pas certifié par cette opération.",
            "legal_review": "Le document reste en review jusqu'à la relecture humaine et ne doit pas être republié automatiquement.",
            "source_typography": "Les irrégularités imprimées dans l'original sont transcrites sans réécriture juridique.",
        },
        "target": target,
    }


def main() -> int:
    plan = build_plan()
    OUTPUT.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{OUTPUT}\n"
        f"{len(plan['target']['nodes'])} divisions, "
        f"{len(plan['target']['articles'])} unités de contenu, "
        f"{sum(len(article['content']) for article in plan['target']['articles'])} caractères."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
