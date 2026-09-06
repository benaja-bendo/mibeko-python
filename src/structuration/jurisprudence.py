"""Structuration de la jurisprudence CCJA (mibeko-python#19).

Une décision de justice devient un `legal_documents` FLUX à UN SEUL article
(le texte intégral) : elle n'a pas de numérotation d'articles propre à
segmenter, contrairement à une loi. `titre_officiel` reprend la convention
déjà établie pour les actes en abrégé du JO (type + numéro + date, fidèle à
la source, docs/decisions.md 2026-08-16) : « Arrêt CCJA n° 163/2023 du 13
juillet 2023 ». `libelle_descriptif` porte les parties (dérivé du texte,
source='article'), à côté du titre — jamais à sa place.

Les citations d'articles que la décision contient sont extraites et posées
dans `jurisprudence_citations`, résolues vers le corpus quand elles
désignent un Acte uniforme qui s'y trouve (`cited_article_id`), sinon
conservées en texte seul (droit national d'un État membre, Code civil...).
"""

from __future__ import annotations

import datetime
import html
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from psycopg2.extras import DateRange
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
from src.api.main import build_document_key, extract_french_date
from src.db.models import Article, ArticleVersion, JurisprudenceCitation, LegalDocument

TYPE_CODE = "JURIS"

# "Arrêt N° 163/2023 du 13 juillet 2023" apparaît toujours en tête du corps —
# fiable et déjà présent, pas besoin de reparser la page de résultats.
NUMERO_ET_DATE_RE = re.compile(
    r"Arrêt\s+N°\s*(?P<numero>\d+/\d{4})\s+du\s+(?P<date_texte>\d{1,2}\s+\S+\s+\d{4})",
    re.IGNORECASE,
)
CHAMBRE_RE = re.compile(r"(?P<chambre>Première|Deuxième|Troisième|Quatrième)\s+chambre", re.IGNORECASE)

# "l'article 301 de l'Acte uniforme portant sur le droit commercial général",
# "l'article 23, alinéa 2 de l'Acte uniforme portant organisation des sûretés".
CITATION_AU_RE = re.compile(
    r"l['’]articles?\s+(?P<numero>\d+)(?:[^.;]{0,40}?)\s+de\s+l['’]Acte\s+uniforme\s+"
    # Bornée à 120 caractères (les intitulés réels du corpus, même les plus
    # longs, tiennent largement dedans) : le texte source enchaîne souvent
    # plusieurs dizaines de mots sans virgule ni point après le libellé
    # ("... en ce qu'il a distingué la convention de prêt liant..."), donc ne
    # PAS borner capturait tout l'exposé du moyen. `resolve_au_article`
    # travaille par préfixe : peu importe qu'il reste un peu de bavardage
    # après le vrai libellé tant que le début est intact.
    r"(?P<libelle>(?:portant|relatif\s+à|organisant)\s+[^,;.]{1,120})",
    re.IGNORECASE,
)
# Hors périmètre par construction (droit national d'un État membre) : gardé
# en texte seul, jamais résolu vers le corpus.
CITATION_CODE_NATIONAL_RE = re.compile(
    r"articles?\s+\d+(?:\s*(?:,|et)\s*\d+)*\s+(?:du|de)\s+Code\s+[^,;.]+",
    re.IGNORECASE,
)


class ExtractedCitation(BaseModel):
    reference_brute: str
    acte_libelle: Optional[str] = None
    numero_article: Optional[str] = None


class ParsedArret(BaseModel):
    numero: Optional[str] = None
    date_decision: Optional[datetime.date] = None
    chambre: Optional[str] = None
    texte_integral: str


def _extract_article_html_bytes(raw: bytes) -> bytes:
    match = re.search(rb"<article[^>]*>(.*?)</article>", raw, re.DOTALL)
    return match.group(1) if match else raw


def _tag_replacement(match: re.Match) -> bytes:
    """Un espace, SAUF quand la balise s'est insérée en plein milieu d'une
    séquence UTF-8 multi-octets (l'octet qui suit est un octet de
    continuation, 0x80-0xBF) : constaté le 06/09/2026 sur juricaf.org,
    l'apostrophe typographique de « Côte d'Ivoire » coupée en deux par un
    `<p>` littéral. Un espace y romprait la séquence pour de bon ; la
    retirer sans rien laisser la reconstitue avant décodage.
    """
    raw = match.string
    end = match.end()
    if end < len(raw) and 0x80 <= raw[end] <= 0xBF:
        return b""
    return b" "


def html_to_text(raw: bytes) -> str:
    """Convertit le HTML brut d'un arrêt en texte simple."""
    article_bytes = _extract_article_html_bytes(raw)
    without_scripts = re.sub(rb"<script\b[^>]*>.*?</script>", b" ", article_bytes, flags=re.DOTALL | re.IGNORECASE)
    without_styles = re.sub(rb"<style\b[^>]*>.*?</style>", b" ", without_scripts, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(rb"<[^>]+>", _tag_replacement, without_styles)
    text = stripped.decode("utf-8", errors="replace")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_arret_html(raw: bytes) -> ParsedArret:
    """Extrait le texte intégral et les métadonnées d'en-tête d'un arrêt."""
    texte = html_to_text(raw)

    numero = None
    date_decision = None
    match = NUMERO_ET_DATE_RE.search(texte)
    if match:
        numero = match.group("numero")
        date_decision = extract_french_date(match.group("date_texte"))

    chambre_match = CHAMBRE_RE.search(texte)
    chambre = f"{chambre_match.group('chambre')} chambre" if chambre_match else None

    return ParsedArret(numero=numero, date_decision=date_decision, chambre=chambre, texte_integral=texte)


def extract_citations(texte_integral: str) -> List[ExtractedCitation]:
    """Extrait les citations d'articles présentes dans le texte d'un arrêt.

    Heuristique par expressions régulières, pas une grammaire complète du
    français juridique : couvre les deux formulations observées sur un
    échantillon réel (arrêt CCJA n° 163/2023) — à élargir au fil des lots
    réellement traités plutôt que de deviner d'autres tournures à l'avance.
    """
    citations: List[ExtractedCitation] = []
    seen: set[str] = set()

    for match in CITATION_AU_RE.finditer(texte_integral):
        reference_brute = " ".join(match.group(0).split())
        if reference_brute in seen:
            continue
        seen.add(reference_brute)
        citations.append(
            ExtractedCitation(
                reference_brute=reference_brute,
                acte_libelle=" ".join(match.group("libelle").split()),
                numero_article=match.group("numero"),
            )
        )

    for match in CITATION_CODE_NATIONAL_RE.finditer(texte_integral):
        reference_brute = " ".join(match.group(0).split())
        if reference_brute in seen:
            continue
        seen.add(reference_brute)
        citations.append(ExtractedCitation(reference_brute=reference_brute))

    return citations


def _au_suffix(titre_officiel: str) -> str:
    """« Acte uniforme portant sur le droit commercial général (révisé) » →
    « portant sur le droit commercial général » — la qualification entre
    parenthèses ('révisé', 'AUDCIF'...) n'est jamais reprise par une citation."""
    match = re.search(r"Acte\s+uniforme\s+(.+)", titre_officiel, re.IGNORECASE)
    suffix = match.group(1) if match else titre_officiel
    suffix = re.sub(r"\s*\([^)]*\)\s*$", "", suffix).strip()
    return suffix


def resolve_au_article(db: Session, acte_libelle: str, numero_article: str) -> Optional[uuid.UUID]:
    """Cherche l'article cité dans le corpus (Acte uniforme publié).

    Le libellé extrait déborde souvent sur la prose qui suit dans le texte
    source (voir `CITATION_AU_RE`) : plutôt que de deviner où il s'arrête, on
    vérifie qu'il COMMENCE par l'intitulé réel d'un des actes uniformes du
    corpus (une dizaine seulement, tous chargés ici) — le reste, quel qu'il
    soit, n'invalide pas la correspondance. Match le plus long en tête
    d'abord, pour ne pas confondre deux actes dont l'un est préfixe de
    l'autre.
    """
    normalized = " ".join(acte_libelle.split()).lower()
    actes_uniformes = (
        db.query(LegalDocument)
        .filter(LegalDocument.type_code == "AU")
        .filter(LegalDocument.deleted_at.is_(None))
        .all()
    )

    candidats = [
        document for document in actes_uniformes if normalized.startswith(_au_suffix(document.titre_officiel).lower())
    ]
    if not candidats:
        return None
    document = max(candidats, key=lambda d: len(_au_suffix(d.titre_officiel)))

    article = (
        db.query(Article)
        .filter(Article.document_id == document.id)
        .filter(Article.numero_article == numero_article)
        .filter(Article.deleted_at.is_(None))
        .first()
    )
    return article.id if article else None


def structure_arret(db: Session, entry: ManifestEntry, data_dir: Path) -> Dict[str, Any]:
    """Structure un arrêt CCJA déjà acquis (HTML sur disque) et l'insère en base.

    Idempotent par `document_key`, comme le reste de la structuration : un
    arrêt déjà présent renvoie `deja_existant` sans rien réécrire.
    """
    html_path = data_dir / entry.fichier
    try:
        raw = html_path.read_bytes()
    except OSError as exc:
        return {"statut": "erreur", "document_id": None, "motif": f"lecture du HTML en échec : {exc}"}

    parsed = parse_arret_html(raw)
    if not parsed.numero or not parsed.texte_integral:
        return {"statut": "erreur", "document_id": None, "motif": "numéro d'arrêt ou texte introuvable dans le HTML"}

    date_txt = f" du {parsed.date_decision.strftime('%d/%m/%Y')}" if parsed.date_decision else ""
    titre_officiel = f"Arrêt CCJA n° {parsed.numero}{date_txt}"
    document_key = build_document_key("FLUX", None, titre_officiel)

    existing = db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first()
    if existing:
        return {"statut": "deja_existant", "document_id": existing.id, "motif": None}

    parties = _extraire_parties(parsed.texte_integral)

    try:
        document = LegalDocument(
            titre_officiel=titre_officiel,
            document_key=document_key,
            document_role="FLUX",
            type_code=TYPE_CODE,
            legal_scope="ohada",
            date_signature=parsed.date_decision,
            date_publication=parsed.date_decision,
            curation_status="draft",
            extraction_status="completed",
            libelle_descriptif=parties,
            libelle_descriptif_source="article" if parties else None,
            metadata_={
                "source_url": entry.source_url,
                "fetched_at": entry.fetched_at,
                "sha256": entry.sha256,
                "chambre": parsed.chambre,
            },
        )
        db.add(document)
        db.flush()

        article = Article(
            document_id=document.id,
            numero_article=parsed.numero,
            ordre_affichage=0,
            validation_status="pending",
        )
        db.add(article)
        db.flush()

        version = ArticleVersion(
            article_id=article.id,
            contenu_texte=parsed.texte_integral,
            validity_period=_daterange_depuis(parsed.date_decision),
            source_locator={"source_url": entry.source_url},
            validation_status="pending",
        )
        db.add(version)

        citations_posees = 0
        for citation in extract_citations(parsed.texte_integral):
            cited_article_id = None
            if citation.acte_libelle and citation.numero_article:
                cited_article_id = resolve_au_article(db, citation.acte_libelle, citation.numero_article)
            db.add(
                JurisprudenceCitation(
                    decision_id=document.id,
                    cited_article_id=cited_article_id,
                    reference_brute=citation.reference_brute,
                )
            )
            citations_posees += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"statut": "structure", "document_id": document.id, "motif": None, "citations": citations_posees}


def run_juricaf_structure(db: Session, data_dir: Path, limit: Optional[int] = None) -> Dict[str, Any]:
    """Structure les arrêts acquis (`data/manifests/juricaf.jsonl`) restés au
    statut `telecharge` — pas de stage `parse` intermédiaire ici (le HTML
    n'a pas besoin de triage natif/MinerU, contrairement à un PDF).
    Reprenable comme `run_batch` : le manifeste est sauvegardé après CHAQUE
    arrêt, jamais en fin de lot.
    """
    manifest_path = data_dir / "manifests" / "juricaf.jsonl"
    manifest = Manifest(manifest_path)
    summary: Dict[str, Any] = {"structures": 0, "deja_existants": 0, "erreurs": []}
    traites = 0

    for entry in manifest.iter_entries():
        if entry.statut != "telecharge":
            continue
        if limit is not None and traites >= limit:
            break

        result = structure_arret(db, entry, data_dir)
        traites += 1

        if result["statut"] == "erreur":
            entry.statut = "erreur"
            entry.add_event("erreur", "MibekoBot/juricaf-structure", detail=result["motif"])
            summary["erreurs"].append({"id": entry.id, "erreur": result["motif"]})
        else:
            entry.statut = "structure"
            entry.add_event("structure", "MibekoBot/juricaf-structure", detail=str(result["document_id"]))
            if result["statut"] == "deja_existant":
                summary["deja_existants"] += 1
            else:
                summary["structures"] += 1

        manifest.save()

    return summary


def _extraire_parties(texte_integral: str) -> Optional[str]:
    """Dérive un libellé descriptif des parties, depuis le corps de l'arrêt.

    « Affaire : X Conseil : ... Contre Y » est le motif constant observé —
    on ne garde que X/Y, jamais les mentions de conseil (avocats), qui
    n'identifient pas l'affaire elle-même.
    """
    match = re.search(
        r"Affaire\s*:\s*(?P<demandeur>.+?)\s+Contre\s+(?P<defendeur>.+?)(?=\s+Arrêt\s+N°|\s+La\s+Cour)",
        texte_integral,
    )
    if not match:
        return None
    # `defendeur` peut concaténer plusieurs co-défendeurs (un seul « Contre »
    # dans le texte source pour N parties) : collapse des espaces après le
    # retrait des mentions de conseil, sinon leur suppression en laisse un
    # blanc double bien visible entre deux noms de partie.
    demandeur = " ".join(re.sub(r"\(Conseils?\s*:.*?\)", "", match.group("demandeur")).split())
    defendeur = " ".join(re.sub(r"\(Conseils?\s*:.*?\)", "", match.group("defendeur")).split())
    if not demandeur or not defendeur:
        return None
    return f"{demandeur} c/ {defendeur}"


def _daterange_depuis(date_decision: Optional[datetime.date]) -> DateRange:
    debut = date_decision or datetime.datetime.utcnow().date()
    return DateRange(debut, None)
