"""Construction de la hiérarchie documentaire, clés déterministes et fusion
de métadonnées — extrait de `src/api/main.py` (mibeko-python#23, § 4 du plan
« boîte de réception », docs/pipeline/plan-boite-de-reception-2026-09.md).

Ces fonctions sont pures (DB + calcul, aucun FastAPI/MinIO/MinerU) : le seul
motif de cette extraction est de casser un couplage d'import. Avant ce
commit, `src/structuration/structurer.py` importait `src.api.main` pour ces
mêmes fonctions — qui instancie l'app FastAPI et importe
`src.services.minio_service`, dont le singleton ouvre une connexion MinIO dès
l'import (`mibeko-python/CLAUDE.md`, pièges connus). Un worker autonome
(mibeko-python#23) héritait donc de ce couplage sans raison : il n'a besoin
que de ce module-ci, jamais de l'app FastAPI.

`src/api/main.py` réimporte ces symboles pour compatibilité des appelants et
tests existants (`from src.services.ingestion import ...`) : c'est la même
fonction, un seul et même calcul, pas une duplication.
"""

from __future__ import annotations

import datetime
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import DateRange
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from src.db.models import Article, ArticleVersion, CurationFlag, LegalDocument, StructureNode
from src.extractor.parser import PAGE_MARKER_PATTERN
from src.extractor.tables import (
    LegalTable,
    TableAnomaly,
    contains_table_markup,
    looks_like_subscription_grid,
    normalize_content,
)

def sanitize_path_component(value: str) -> str:
    """Nettoie une valeur pour produire un segment de chemin stable pour Domino."""

    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "document"


def build_document_key(document_role: str, stock_code: Optional[str], title: str) -> str:
    """Construit une cle metier stable pour eviter les doublons documentaires."""

    if document_role == "STOCK" and stock_code:
        return f"stock:{sanitize_path_component(stock_code)}"

    return f"flux:{sanitize_path_component(title)}"


def build_domino_root(document_role: str, stock_code: Optional[str], document_id: uuid.UUID) -> str:
    """Construit la racine Domino/MinIO d'un document et de ses artefacts."""

    document_scope = sanitize_path_component(stock_code) if stock_code else str(document_id)
    role_scope = "stock" if document_role == "STOCK" else "flux"
    return f"domino/legal-documents/{role_scope}/{document_scope}"


def build_object_key(
    document_role: str,
    stock_code: Optional[str],
    document_id: uuid.UUID,
    area: str,
    filename: str,
    run_id: Optional[uuid.UUID] = None,
) -> str:
    """Construit une cle objet deterministe pour le stockage MinIO/Domino."""

    safe_name = sanitize_path_component(os.path.splitext(filename)[0])
    extension = os.path.splitext(filename)[1].lower()
    base = build_domino_root(document_role, stock_code, document_id)

    if run_id:
        return f"{base}/{area}/{run_id}/{safe_name}{extension}"

    return f"{base}/{area}/{safe_name}{extension}"


def clear_document_structure(db: Session, document_id: uuid.UUID) -> None:
    """Supprime proprement la structure et les articles d’un document avant réimport."""

    db.query(ArticleVersion).filter(
        ArticleVersion.article_id.in_(db.query(Article.id).filter(Article.document_id == document_id))
    ).delete(synchronize_session=False)
    db.query(Article).filter(Article.document_id == document_id).delete(synchronize_session=False)
    db.query(StructureNode).filter(StructureNode.document_id == document_id).delete(synchronize_session=False)
    db.flush()


_DOUBLON_SUFFIX_RE = re.compile(r"^(?P<origine>.*)_doublon_(?P<n>\d+)$")


def derive_numero_origine(numero_article: str) -> Optional[str]:
    """Inverse de la logique de suffixage de `unique_article_number`
    (`ingest_hierarchy`) : retrouve le numéro d'origine d'un `numero_article`
    déjà suffixé ``_doublon_N``, ou ``None`` s'il n'est pas suffixé.

    Utilisé par `scripts/backfill_doublon_flags.py` (audit
    docs/audit-ingestion-2026-08-02.md, phase 2) pour rattraper les articles
    déjà renommés par une ingestion antérieure au correctif, sans re-parser
    ni re-ingérer quoi que ce soit.
    """
    match = _DOUBLON_SUFFIX_RE.match(numero_article)
    return match.group("origine") if match else None


_LATIN_SUB_SUFFIX_RE = re.compile(r"\b(?:bis|ter|quater|quinquies|sexies|septies)\b", re.IGNORECASE)
_HYPHEN_SUB_NUMBER_RE = re.compile(r"-\d+$")
_DECIMAL_SUB_NUMBER_RE = re.compile(r"^\d+\.\d+")
_LETTERED_SUB_ITEM_RE = re.compile(r"\d\s+[a-z]\)?$", re.IGNORECASE)


def ordinal_from_raw_number(raw_number: str) -> Optional[int]:
    """Numéro ordinal d'un article pour le contrôle de séquence, à partir de
    son numéro D'ORIGINE (pas encore suffixé `_doublon_N` — appliquer
    `derive_numero_origine` d'abord si nécessaire) :

    - « premier » → 1 (forme légale officielle, non normalisée à l'affichage
      où `numero_article` reste « premier ») ;
    - « X nouveau » → hors séquence, ``None`` : c'est le texte de remplacement
      cité dans un acte modificatif, pas un article séquentiel (sinon faux
      doublon avec l'article d'exécution de même numéro) ;
    - « 66 bis », « 16-1 », « 1.11 », « 14 a) » → hors séquence, ``None``
      également : insertions ultérieures ou sous-paragraphes lettrés
      légitimes (Code civil, Code Pénal, règlements CEMAC à numérotation
      décimale) qui partagent le numéro DE BASE d'un article déjà existant
      sans en être des doublons — remédiation 2026-08-02 phase 5, audit
      ingestion 652→346 flags `article_doublon` dont 285 sur 3 codes STOCK
      relevaient exactement de ce schéma. Seul le numéro de base (« 66 »,
      « 16 », « 1 », « 14 ») reste dans la séquence : le contrôle de trous
      (`find_missing_runs`) continue de le voir présent, la détection de
      doublons ne voit plus jamais ses insertions comme des répétitions du
      même ordinal.

    Extrait de `ingest_hierarchy` (`insert_nodes`) pour être réutilisé sans
    duplication par tout script qui reconstruit une séquence d'articles déjà
    en base (ex. `scripts/reclassify_embedded_series_flags.py`).
    """
    low_number = raw_number.lower()
    if "nouveau" in low_number or "nouvelle" in low_number:
        return None
    if low_number.startswith(("premier", "première", "premiere")):
        return 1
    if (
        _LATIN_SUB_SUFFIX_RE.search(raw_number)
        or _HYPHEN_SUB_NUMBER_RE.search(raw_number)
        or _DECIMAL_SUB_NUMBER_RE.match(raw_number)
        or _LETTERED_SUB_ITEM_RE.search(raw_number)
    ):
        return None
    seq_match = re.match(r"(\d+)", raw_number)
    return int(seq_match.group(1)) if seq_match else None


def find_missing_runs(distinct_sorted: List[int]) -> List[Tuple[int, int, int]]:
    """Plages d'entiers ABSENTES dans [min, max] de l'ensemble fourni.

    Basé sur l'ENSEMBLE (pas la séquence) : robuste à un OCR qui restitue les
    pages dans le désordre — un trou « 487→552 » réel est détecté quel que soit
    l'ordre d'apparition des articles. Chaque plage : (début, fin, taille).
    """
    runs: List[Tuple[int, int, int]] = []
    if not distinct_sorted:
        return runs

    present = set(distinct_sorted)
    lo, hi = distinct_sorted[0], distinct_sorted[-1]
    start: Optional[int] = None
    for value in range(lo, hi + 1):
        if value not in present:
            if start is None:
                start = value
        elif start is not None:
            runs.append((start, value - 1, value - start))
            start = None
    return runs


def count_series_restarts(ordinals: List[int], restart_low: int = 3, restart_prev_min: int = 10) -> int:
    """Compte les redémarrages de numérotation (chute vers un petit numéro après
    un numéro élevé) : signal d'une compilation de plusieurs textes dont chacun
    renumérote ses articles à partir de 1."""
    restarts = 0
    prev: Optional[int] = None
    for number in ordinals:
        if prev is not None and number <= restart_low and prev > restart_prev_min:
            restarts += 1
        prev = number
    return restarts


def find_embedded_series_runs(
    ordinals: List[int],
    restart_low: int = 3,
    min_run_length: int = 3,
) -> List[Tuple[int, int]]:
    """Repère une série secondaire INCRUSTÉE, manquée par `count_series_restarts`
    faute d'atteindre son seuil absolu `restart_prev_min` (calibré pour des
    recueils longs — un acte principal COURT (loi de ratification à 2-3
    articles, arrêté bref) suivi d'une annexe qui renumérote à partir de 1
    (traité ratifié, cahier des charges) ne fait jamais dépasser ce seuil au
    sommet atteint avant la chute — audit 2026-08-02 phase 4, remédiation :
    652 signalements `article_doublon` sur 70 documents, dont la majorité
    suivait exactement ce schéma sur 6 échantillons vérifiés (1959 à 2025).

    Signal retenu, indépendant de l'ampleur absolue : une VRAIE chute (`prev`
    STRICTEMENT supérieur — un doublon immédiat où les deux valeurs sont
    égales n'en est pas une) vers une petite valeur, confirmée seulement si ce
    qui suit reprend une ascension soutenue d'au moins `min_run_length`
    valeurs. Sans cette confirmation, ce n'est pas une série qui s'installe
    mais une réapparition isolée (erratum, doublon OCR) — qui doit rester
    signalée normalement (cf. audit phase 2 : doublons non consécutifs,
    `test_doublon_non_consecutif_detecte_hors_compilation`).

    Renvoie les plages (index début, index fin inclus, dans `ordinals`) des
    séries ainsi confirmées.
    """
    runs: List[Tuple[int, int]] = []
    i = 1
    n = len(ordinals)
    while i < n:
        prev, number = ordinals[i - 1], ordinals[i]
        if number <= restart_low and prev > number:
            j = i
            while j + 1 < n and ordinals[j + 1] > ordinals[j]:
                j += 1
            if j - i + 1 >= min_run_length:
                runs.append((i, j))
                i = j + 1
                continue
        i += 1
    return runs


def analyze_article_sequence(
    sequence: List[Tuple[Optional[int], uuid.UUID]],
    *,
    block_threshold: int = 10,
    restart_low: int = 3,
    restart_prev_min: int = 10,
    min_restarts: int = 2,
    min_embedded_series_run: int = 3,
    max_anomalies: int = 200,
    already_flagged: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Analyse PURE d'une séquence d'articles (ordinal, id) → anomalies à signaler.

    Vise le RAPPEL sur les vrais défauts d'ingestion tout en restant robuste à un
    OCR désordonné (où l'analyse séquentielle des trous produit des faux positifs
    massifs : « saut de 86 à 553 » alors que l'article 553 a juste été restitué
    trop tôt). Trois familles d'anomalies :

    - ``article_doublon`` : un même numéro ordinal apparaît plusieurs fois. Hors
      compilation, TOUTE réapparition compte (consécutive ou non — audit
      docs/audit-ingestion-2026-08-02.md phase 2 : « 12 » puis, bien plus loin,
      « 12 bis » partagent le même ordinal sans être adjacents dans la séquence
      ni entrer en collision de CHAÎNE, donc jamais renommés par
      ``unique_article_number``). En compilation, seuls les doublons CONSÉCUTIFS
      restent signalés : la réapparition d'un numéro sur un acte encapsulé
      différent est le signal normal d'une nouvelle série, pas une erreur — tout
      signaler produirait exactement le bruit que cette fonction évite déjà pour
      les petits trous (cf. plus bas). Hors compilation *au sens de
      `count_series_restarts`*, une série secondaire INCRUSTÉE peut cependant
      être confirmée par `find_embedded_series_runs` (acte principal trop
      court pour franchir `restart_prev_min`, ex. loi de ratification à 2
      articles + traité annexé qui renumérote) : ses réapparitions ne sont
      alors PAS comptées comme doublons, un seul ``compilation_suspectee``
      les remplace (remédiation 2026-08-02 phase 4) ;
    - ``compilation_suspectee`` : la numérotation redémarre plusieurs fois
      (plusieurs séries) → le document devrait être segmenté avant publication ;
    - ``bloc_manquant`` / ``article_manquant`` : plages absentes calculées sur
      l'ENSEMBLE des numéros (indépendant de l'ordre). Un bloc contigu long
      (≥ ``block_threshold``) est signalé même en compilation (peu probable que ce
      soit un acte encapsulé, lesquels sont peu numérotés) ; les petits trous ne
      sont signalés QUE hors compilation (sinon bruit dû à l'entrelacement des
      séries d'une compilation).

    ``already_flagged`` : ensemble d'``article_id`` déjà couverts par un
    `CurationFlag` `article_doublon` distinct (collision de CHAÎNE détectée et
    renommée par ``unique_article_number`` — cf. `ingest_hierarchy`). Ces
    articles sont exclus de la détection ci-dessous pour éviter un double
    signalement du même défaut (mission « corrections post-audit » phase 2 :
    un flag par collision, pas deux).

    Renvoie une liste de dicts {type_probleme, article_id, description}.
    """
    anomalies: List[Dict[str, Any]] = []
    already_flagged = already_flagged or set()

    # `filtered` et `ordinals` restent alignés index à index (indispensable
    # pour reprojeter les plages de `find_embedded_series_runs`, calculées sur
    # `ordinals`, vers les `article_id` correspondants).
    filtered = [(number, article_id) for number, article_id in sequence if number is not None]
    ordinals = [number for number, _ in filtered]
    if not ordinals:
        return anomalies[:max_anomalies]

    # 1. Compilation (plusieurs séries de numérotation) ? Calculé AVANT les
    # doublons : leur détection en dépend (cf. docstring).
    restarts = count_series_restarts(ordinals, restart_low, restart_prev_min)
    is_compilation = restarts >= min_restarts
    embedded_runs: List[Tuple[int, int]] = (
        [] if is_compilation else find_embedded_series_runs(ordinals, restart_low, min_embedded_series_run)
    )

    # 2. Doublons.
    if is_compilation:
        # Cœur inchangé : seuls les doublons consécutifs.
        prev: Optional[int] = None
        for number, article_id in sequence:
            if number is None:
                continue
            if prev is not None and number == prev and article_id not in already_flagged:
                anomalies.append({
                    "type_probleme": "article_doublon",
                    "article_id": article_id,
                    "description": f"Numéro d'article {number} répété consécutivement.",
                })
            prev = number
    else:
        # Hors compilation, un ordinal ne devrait apparaître qu'une fois où
        # qu'il soit dans la séquence — SAUF s'il appartient à une série
        # secondaire incrustée confirmée (`embedded_runs`) : celle-ci est
        # signalée une seule fois, globalement, plus bas.
        embedded_indices = {i for start, end in embedded_runs for i in range(start, end + 1)}
        seen_ordinals: Dict[int, uuid.UUID] = {}
        for idx, (number, article_id) in enumerate(filtered):
            if idx in embedded_indices:
                seen_ordinals[number] = article_id
                continue
            if number in seen_ordinals:
                if article_id not in already_flagged:
                    anomalies.append({
                        "type_probleme": "article_doublon",
                        "article_id": article_id,
                        "description": f"Numéro d'article {number} déjà rencontré ailleurs dans ce document.",
                    })
            else:
                seen_ordinals[number] = article_id

        for start, end in embedded_runs:
            anomalies.append({
                "type_probleme": "compilation_suspectee",
                "article_id": None,
                # 'warning' et non 'blocking' (défaut) : contrairement à une VRAIE
                # compilation multi-actes (cf. plus bas), une série incrustée
                # unique est le signal normal d'une annexe — informe sans
                # bloquer la publication (seul 'blocking' bloque côté Laravel).
                "severity": "warning",
                "description": (
                    f"La numérotation redémarre à l'article {ordinals[start]} après l'article "
                    f"{ordinals[start - 1]} et reprend une série de {end - start + 1} articles : "
                    "texte secondaire probable (annexe, traité ratifié, cahier des charges) plutôt "
                    "qu'un doublon. Vérifier si une segmentation est nécessaire avant publication."
                ),
            })

    if is_compilation:
        anomalies.append({
            "type_probleme": "compilation_suspectee",
            "article_id": None,
            "description": (
                f"La numérotation d'articles redémarre {restarts} fois "
                "(plusieurs séries détectées) : compilation probable de plusieurs "
                "textes. Segmentation recommandée avant publication."
            ),
        })

    # 3. Plages absentes (basé sur l'ensemble, robuste à l'ordre).
    for start, end, size in find_missing_runs(sorted(set(ordinals))):
        if size >= block_threshold:
            anomalies.append({
                "type_probleme": "bloc_manquant",
                "article_id": None,
                "description": (
                    f"{size} numéros d'articles consécutifs absents ({start}-{end}) : "
                    "perte de pages probable à l'extraction."
                ),
            })
        elif not is_compilation:
            label = f"{start}" if start == end else f"{start}-{end}"
            anomalies.append({
                "type_probleme": "article_manquant",
                "article_id": None,
                "description": f"Article(s) {label} absent(s) ({size} numéro(s)).",
            })

    return anomalies[:max_anomalies]


def flag_article_sequence_anomalies(
    db: Session,
    document_id: uuid.UUID,
    sequence: List[Tuple[Optional[int], uuid.UUID]],
    max_flags: int = 200,
    already_flagged: Optional[set] = None,
) -> int:
    """Persiste les anomalies de numérotation d'un document en `curation_flags`.

    Garde-fou de curation : tant que ces anomalies ne sont pas résolues, le
    document ne peut pas être publié (cf. `LegalDocumentController`). L'analyse est
    déléguée à `analyze_article_sequence` (pure, testée). Renvoie le nb d'anomalies.

    ``already_flagged`` : cf. `analyze_article_sequence` — articles déjà
    flagués par `unique_article_number` (collision de chaîne renommée), à ne
    pas re-signaler ici pour la même collision vue sous l'angle ordinal.
    """
    # Idempotence : on purge nos propres signalements heuristiques NON résolus
    # avant de recalculer (ré-import/replay), sans toucher aux flags résolus par un
    # humain ni aux autres couches (structural/llm). Les flags article-liés
    # partent déjà en CASCADE quand l'article est supprimé ; ceci couvre en plus
    # les flags niveau document (article_id NULL : bloc manquant, compilation).
    # NB : les flags de collision de `unique_article_number` (posés juste avant
    # cet appel, cf. `ingest_hierarchy`) sont volontairement `source="heuristic"`
    # eux aussi — non purgés ici : cette requête ne vise QUE ceux déjà en base
    # avant l'appel courant (rejeu), les flags fraîchement ajoutés dans cette
    # même transaction n'y sont pas encore visibles côté SQL.
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document_id,
        CurationFlag.source == "heuristic",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)

    anomalies = analyze_article_sequence(sequence, max_anomalies=max_flags, already_flagged=already_flagged)
    for anomaly in anomalies:
        db.add(CurationFlag(
            document_id=document_id,
            article_id=anomaly["article_id"],
            source="heuristic",
            type_probleme=anomaly["type_probleme"],
            # Défaut du modèle ('blocking') sauf anomalie explicitement moins
            # sévère (ex. série incrustée confirmée — cf. `analyze_article_sequence`).
            severity=anomaly.get("severity", "blocking"),
            description=anomaly["description"],
        ))
    return len(anomalies)


def assign_dfs_order(hierarchy: List[Dict[str, Any]]) -> None:
    """Assigne un ordre d'affichage GLOBAL (pré-ordre DFS) à chaque nœud.

    Écrit la clé ``_order`` (entier croissant unique sur tout le document) sur
    chaque nœud, dans l'ordre de lecture (un parent précède ses enfants, qui
    précèdent le frère suivant du parent). Sans cela, ``ordre_affichage`` /
    ``sort_order`` ne sont monotones qu'au sein d'un groupe de frères (reset à 0
    par branche) : un listing à plat ``ORDER BY ordre_affichage`` ressort alors
    mélangé. Le viewer arborescent (qui regroupe par parent) reste correct dans
    les deux cas, mais une clé globale rend le modèle robuste pour tout
    consommateur à plat (API d'extraction paginée, exports).
    """

    counter = {"n": 0}

    def walk(nodes: List[Dict[str, Any]]) -> None:
        for node in nodes:
            node["_order"] = counter["n"]
            counter["n"] += 1
            children = node.get("children")
            if children:
                walk(children)

    walk(hierarchy)


def ingest_hierarchy(
    db: Session,
    document: LegalDocument,
    hierarchy: List[Dict[str, Any]],
    run_id: Optional[uuid.UUID] = None,
    media_id: Optional[uuid.UUID] = None,
    validation_status: str = "pending",
) -> None:
    """Insère une hiérarchie parsée dans `structure_nodes`, `articles` et `article_versions`.

    GARDE-FOU (dashboard#166, 19/09/2026) — vérifié en production le même
    jour : `article_versions.validity_period` doit rester une date de DROIT
    (un amendement légal réel, tracé par `modifie_par_document_id` côté
    Laravel), jamais une date de PIPELINE. `clear_document_structure()`
    ci-dessous supprime tout, et chaque article reçoit un `node_id` (donc un
    id) fraîchement tiré au sort : une réingestion ne peut PAS forker une
    version sur un article existant, elle en pose une neuve, datée
    honnêtement de cette ingestion (déjà documenté par `getValidityStartAttribute()`
    côté Laravel : cette date reflète l'enregistrement, pas une entrée en
    vigueur garantie). C'est la seule raison pour laquelle ce mécanisme n'a
    jamais produit les 2 486 fausses versions mesurées ce jour-là — leur
    origine réelle était `FusionnerFragmentsCommand` (mibeko-tableau-de-bord),
    corrigée dans la même passe.

    N'AJOUTEZ JAMAIS ici (ni ailleurs dans ce fichier) de chemin qui
    retrouverait un `Article` déjà existant pour lui insérer une DEUXIÈME
    ligne `article_versions` sans passer par une décision humaine explicite
    d'amendement (texte modificateur + date d'effet obligatoires, doctrine
    posée côté Laravel) — ce serait exactement reproduire le défaut corrigé.
    """

    clear_document_structure(db, document.id)
    seen_article_numbers: Dict[str, int] = {}
    rename_collisions: List[Dict[str, Any]] = []
    table_counter = {"n": 0}
    article_sequence: List[Tuple[Optional[int], uuid.UUID]] = []
    table_findings: List[Dict[str, Any]] = []

    def leaf_content(
        node_data: Dict[str, Any],
        locator: Dict[str, Any],
        article_id: uuid.UUID,
    ) -> str:
        """Contenu d'une feuille, débarrassé de tout balisage de tableau.

        Invariant du corpus : `contenu_texte` est du texte pur. Les tableaux y
        sont linéarisés et leur forme canonique part dans `source_locator`
        (`docs/decisions.md`, 09/08/2026). S'applique à TOUTES les feuilles, pas
        aux seuls nœuds TABLEAU : la production porte des tableaux incrustés
        dans des articles ordinaires (arrêtés miniers, coordonnées de permis).

        Les anomalies relevées sont mises de côté ici et transformées en
        `CurationFlag` une fois les articles insérés — un flag exige un
        `article_id`, qui n'existe pas encore à ce stade.
        """
        content = node_data.get("content", "") or ""
        if not contains_table_markup(content):
            return content

        normalized, tables, anomalies = normalize_content(content)
        if tables:
            locator["tables"] = [table.to_locator() for table in tables]
        if anomalies or tables:
            table_findings.append(
                {"article_id": article_id, "tables": tables, "anomalies": anomalies}
            )
        return normalized

    def unique_article_number(base: str, node_id: uuid.UUID) -> str:
        """Garantit l'unicité de (document_id, numero_article).

        Suffixe ``_doublon_N`` en cas de collision — la contrainte unique
        uq_articles_document_numero l'exige. S'applique aux vrais articles
        homonymes MAIS AUSSI aux feuilles PREAMBULE/SIGNATURE multiples : un acte
        compilé (Code bleu, recueil) peut contenir plusieurs préambules/signatures.

        Le renommage n'est JAMAIS silencieux (audit
        docs/audit-ingestion-2026-08-02.md, phase 2 : sur les 62 documents
        rescindés en phase 1, 5 525 renommages n'avaient donné lieu qu'à 315
        signalements individuels) : chaque collision est enregistrée dans
        ``rename_collisions``, transformée en `CurationFlag` `article_doublon`
        une fois l'article inséré (cf. fin de fonction), numéro d'origine
        conservé dans la description.
        """
        if base in seen_article_numbers:
            seen_article_numbers[base] += 1
            final = f"{base}_doublon_{seen_article_numbers[base]}"
            rename_collisions.append({"article_id": node_id, "numero_origine": base, "numero_final": final})
            return final
        seen_article_numbers[base] = 0
        return base

    def insert_nodes(nodes_list: List[Dict[str, Any]], parent_tree_path: Optional[str] = None, parent_node_id: Optional[uuid.UUID] = None) -> None:
        for node_data in nodes_list:
            # Ordre d'affichage GLOBAL (pré-ordre DFS) calculé par assign_dfs_order.
            display_order = node_data["_order"]
            node_id = uuid.uuid4()
            # Ltree labels must start with a letter. We prefix with 'n' (node)
            node_ltree_id = f"n_{str(node_id).replace('-', '_')}"
            current_tree_path = f"{parent_tree_path}.{node_ltree_id}" if parent_tree_path else node_ltree_id
            ltree_obj = Ltree(current_tree_path)

            if node_data["type"] == "ARTICLE":
                raw_number = str(node_data.get("number", "")).strip()
                article_number = unique_article_number(raw_number or f"SANS_NUM_{str(uuid.uuid4())[:8]}", node_id)

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=article_number,
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                # Numéro ordinal pour le contrôle de séquence (cf. `ordinal_from_raw_number`).
                article_sequence.append((ordinal_from_raw_number(raw_number), article.id))

                # `page_end` (plage de pages, mibeko-python#24 § 3.5) : déjà
                # propagé pour DISPOSITION/NOTE plus bas dans cette même
                # fonction, jamais pour ARTICLE alors que le parseur le
                # calcule pourtant déjà (`LegalDocumentParser`, fermeture de
                # `current_article`) — additif, `page` reste écrit à
                # l'identique pour ne rien casser côté lecteurs existants
                # (front, dashboard).
                article_locator: Dict[str, Any] = (
                    {"page": node_data["page"]} if node_data.get("page") is not None else {}
                )
                if node_data.get("page_end") is not None:
                    article_locator["page_end"] = node_data["page_end"]
                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=leaf_content(node_data, article_locator, article.id),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=article_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "PREAMBULE":
                # Préambule de l'acte (qualité du signataire, visas, considérants) :
                # feuille de tête marquée content_format=preamble dans source_locator
                # (même approche que TABLEAU, zéro migration). Hors contrôle de
                # séquence : ce n'est pas un article numéroté.
                preamble_locator: Dict[str, Any] = {"content_format": "preamble"}
                if node_data.get("page") is not None:
                    preamble_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=unique_article_number("PREAMBULE", node_id),
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=leaf_content(node_data, preamble_locator, article.id),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=preamble_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "SIGNATURE":
                # Formule finale (« Fait à … » + signataire) : feuille de pied
                # marquée content_format=signature (même approche que TABLEAU,
                # zéro migration). Hors contrôle de séquence d'articles.
                signature_locator: Dict[str, Any] = {"content_format": "signature"}
                if node_data.get("page") is not None:
                    signature_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=unique_article_number("SIGNATURE", node_id),
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=leaf_content(node_data, signature_locator, article.id),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=signature_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "TABLEAU":
                # Tableau (grille salariale, etc.) : feuille de contenu marquée
                # content_format=table dans source_locator (option A, zéro migration).
                table_counter["n"] += 1
                table_locator: Dict[str, Any] = {"content_format": "table"}
                if node_data.get("page") is not None:
                    table_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=f"TABLEAU_{table_counter['n']}",
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=leaf_content(node_data, table_locator, article.id),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=table_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] in {"DISPOSITION", "NOTE"}:
                leaf_type = node_data["type"]
                prefix = "DISPOSITION" if leaf_type == "DISPOSITION" else "NOTE"
                raw_number = str(node_data.get("number", "")).strip()
                if not raw_number.startswith(f"{prefix}_"):
                    raw_number = f"{prefix}_{raw_number or display_order}"

                locator: Dict[str, Any] = {"content_format": prefix.lower()}
                if node_data.get("page") is not None:
                    locator["page"] = node_data["page"]
                if node_data.get("page_end") is not None:
                    locator["page_end"] = node_data["page_end"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=unique_article_number(raw_number, node_id),
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=leaf_content(node_data, locator, article.id),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=locator,
                    validation_status=validation_status,
                )
                db.add(version)
            else:
                node = StructureNode(
                    id=node_id,
                    document_id=document.id,
                    type_unite=node_data["type"],
                    numero=node_data.get("number", ""),
                    titre=node_data.get("title", ""),
                    tree_path=ltree_obj,
                    sort_order=display_order,
                    validation_status=validation_status,
                )
                db.add(node)
                db.flush()

                if node_data.get("children"):
                    insert_nodes(node_data["children"], parent_tree_path=current_tree_path, parent_node_id=node.id)

    if hierarchy:
        assign_dfs_order(hierarchy)
        insert_nodes(hierarchy)
        # Flush explicite obligatoire ici : la session est `autoflush=False`
        # (src/db/database.py) et `insert_nodes` ne flushe qu'au passage de
        # chaque `StructureNode` (ligne ci-dessus) — les articles/PREAMBULE/
        # SIGNATURE en fin d'arbre, après le dernier `StructureNode`, restent
        # donc en attente jusqu'ici. Sans ce flush, la boucle `rename_collisions`
        # plus bas ajoute des `CurationFlag` référençant CES MÊMES articles
        # encore non persistés → violation de clé étrangère au commit final
        # (constaté sur le Code pénal 1836 : 12 articles jamais flushés, dont 5
        # référencés par un flag de doublon).
        db.flush()
        # `flag_article_sequence_anomalies` PURGE d'abord (requête SQL directe
        # sur les flags heuristiques existants) puis ajoute les siens : les
        # flags de collision ci-dessous sont ajoutés APRÈS cet appel, jamais
        # avant — sinon un autoflush déclenché par la requête de purge les
        # écrirait en base juste à temps pour être supprimés par cette même
        # purge (elle filtre aussi `source="heuristic"`).
        already_renamed_ids = {collision["article_id"] for collision in rename_collisions}
        flag_article_sequence_anomalies(db, document.id, article_sequence, already_flagged=already_renamed_ids)

        # Séries secondaires incrustées confirmées sur la séquence ordinale
        # COMPLÈTE (article_sequence, remplie ci-dessus) : le renommage de
        # chaque collision reste TOUJOURS individuellement flagué — jamais
        # silencieux, audit 2026-08-02 phase 2, cf. test_zero_renommage_sans_flag
        # — mais un renommage qui appartient à une annexe confirmée n'est pas
        # de même nature qu'une vraie collision non résolue : description et
        # sévérité l'indiquent (remédiation phase 4), sans jamais réduire le
        # nombre de flags ni leur bijection avec les renommages.
        filtered_for_embedded = [(n, aid) for n, aid in article_sequence if n is not None]
        ordinals_for_embedded = [n for n, _ in filtered_for_embedded]
        embedded_run_ids = {
            filtered_for_embedded[i][1]
            for start, end in find_embedded_series_runs(ordinals_for_embedded)
            for i in range(start, end + 1)
        }

        for collision in rename_collisions:
            if collision["article_id"] in embedded_run_ids:
                severity = "warning"
                description = (
                    f"Numéro d'article « {collision['numero_origine']} » renommé en "
                    f"« {collision['numero_final']} » : appartient probablement à une série "
                    "secondaire (annexe, traité ratifié, cahier des charges) qui redémarre sa "
                    "propre numérotation — pas nécessairement une erreur, à confirmer."
                )
            else:
                severity = "blocking"
                description = (
                    f"Numéro d'article « {collision['numero_origine']} » en collision avec un autre "
                    f"article du même document — renommé en « {collision['numero_final']} » pour "
                    "respecter la contrainte d'unicité. Nécessite une revue humaine (fusion, "
                    "re-numérotation, ou confirmation qu'il s'agit bien de deux articles distincts)."
                )
            db.add(CurationFlag(
                document_id=document.id,
                article_id=collision["article_id"],
                source="heuristic",
                type_probleme="article_doublon",
                severity=severity,
                description=description,
            ))

    flag_table_anomalies(db, document, table_findings)


def flag_table_anomalies(
    db: Session,
    document: LegalDocument,
    findings: List[Dict[str, Any]],
) -> None:
    """Transforme les constats de normalisation des tableaux en signalements.

    La normalisation, elle, a déjà eu lieu : ces flags ne bloquent pas
    l'ingestion, ils disent à l'éditeur ce qu'il doit aller vérifier contre le
    PDF source. Un tableau dont l'arithmétique ne tombe pas juste porte un
    chiffre mal océrisé — c'est le seul moyen de le savoir sans relire à la main
    des annexes budgétaires de cinquante rangées.

    Une grille d'abonnements du Journal officiel n'est pas un défaut de
    conversion mais un **faux article** : elle est signalée à part, en proposant
    le retrait (`php artisan mibeko:retirer-articles-masthead`), jamais en
    supprimant d'office — la suppression reste une décision humaine.
    """
    for finding in findings:
        anomalies: List[TableAnomaly] = finding["anomalies"]
        tables: List[LegalTable] = finding["tables"]

        for anomaly in anomalies:
            db.add(CurationFlag(
                document_id=document.id,
                article_id=finding["article_id"],
                source="heuristic",
                type_probleme=anomaly.code,
                severity=anomaly.severity,
                description=anomaly.message,
            ))

        if any(looks_like_subscription_grid(table) for table in tables):
            db.add(CurationFlag(
                document_id=document.id,
                article_id=finding["article_id"],
                source="heuristic",
                type_probleme="tableau_ours_journal_officiel",
                severity="warning",
                description=(
                    "Ce tableau est la grille tarifaire d'abonnement au Journal officiel "
                    "(l'« ours »), pas un texte juridique : MinerU l'a capturé comme s'il "
                    "était un article. À retirer du document plutôt qu'à corriger — voir "
                    "`php artisan mibeko:retirer-articles-masthead`."
                ),
            ))


def _contenu_par_page(markdown_text: str) -> Dict[int, str]:
    """Découpe un markdown à marqueurs ``[[MIBEKO_PAGE:N]]`` en {page: contenu}.

    Partagé par les deux mesures de mibeko-python#24 (§ 3.5) : Mesure 1
    (`flag_page_coverage_gaps`, PDF → extraction) et Mesure 2
    (`flag_structure_coverage_gaps`, extraction → structure) posent la même
    question — « cette page a-t-elle du contenu ? » — sur deux objets
    différents (le markdown lui-même, puis la hiérarchie qui en est tirée) ;
    un seul découpage évite que les deux mesures divergent sur ce qu'elles
    considèrent comme une "page".
    """
    pages_content: Dict[int, str] = {}
    current_page: Optional[int] = None
    buffer: List[str] = []
    for line in markdown_text.split("\n"):
        match = PAGE_MARKER_PATTERN.match(line.strip())
        if match:
            if current_page is not None:
                pages_content[current_page] = "\n".join(buffer)
            current_page = int(match.group(1))
            buffer = []
        else:
            buffer.append(line)
    if current_page is not None:
        pages_content[current_page] = "\n".join(buffer)
    return pages_content


def flag_page_coverage_gaps(
    db: Session,
    document_id: uuid.UUID,
    markdown_text: str,
    run_id: Optional[uuid.UUID] = None,
    min_chars_par_page: int = 20,
) -> bool:
    """Émet un flag de curation NON bloquant quand une page manque — ou n'a
    presque aucun contenu — dans la plage propre à CE document (son premier
    au dernier marqueur ``[[MIBEKO_PAGE:N]]``). Mesure 1 de mibeko-python#24
    (§ 3.5 du plan « boîte de réception ») : PDF → extraction.

    Portée à la plage de CE document, JAMAIS au nombre total de pages du PDF
    source : un acte de 2 pages extrait d'un Journal officiel de 50 partage
    le même PDF source que les 48 autres pages, qui appartiennent à d'autres
    actes — les comparer aurait produit une fausse alerte de couverture sur
    la quasi-totalité des actes d'un JO. C'est précisément le défaut trouvé
    par la revue technique du 14/09/2026 dans le plan initial (couverture de
    pages mesurée contre le mauvais objet), qui a fait scinder ce lot en deux
    mesures séparées.

    Idempotent : purge son propre flag non résolu avant de recalculer, comme
    `flag_low_ocr_quality`/`flag_article_sequence_anomalies`.

    Renvoie True si un flag a été émis (au moins une page incomplète dans la
    plage propre au document), False sinon — y compris quand le markdown ne
    porte aucun marqueur de page (hors périmètre de cette mesure : rien à
    comparer).
    """
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document_id,
        CurationFlag.source == "heuristic",
        CurationFlag.type_probleme == "couverture_pages_incomplete",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)

    pages_content = _contenu_par_page(markdown_text)
    if not pages_content:
        return False

    premiere_page = min(pages_content)
    derniere_page = max(pages_content)
    pages_incompletes = [
        page for page in range(premiere_page, derniere_page + 1)
        if len(pages_content.get(page, "").strip()) < min_chars_par_page
    ]

    if not pages_incompletes:
        return False

    liste = ", ".join(str(p) for p in pages_incompletes[:10])
    reste = f" (+{len(pages_incompletes) - 10} autre(s))" if len(pages_incompletes) > 10 else ""
    db.add(CurationFlag(
        document_id=document_id,
        source="heuristic",
        type_probleme="couverture_pages_incomplete",
        severity="warning",
        description=(
            f"{len(pages_incompletes)} page(s) sans contenu exploitable dans la plage propre "
            f"à ce document (pages {premiere_page} à {derniere_page}) : {liste}{reste}. "
            "Vérifier si du contenu a été perdu à l'extraction."
        ),
    ))
    return True


def _plages_de_pages(hierarchy: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """Parcourt récursivement la hiérarchie PARSÉE (avant insertion — mêmes
    dicts que `LegalDocumentParser.parse_hierarchy()` renvoie) et liste les
    plages (page, page_fin) des feuilles portant un contenu réel — miroir de
    ce que `ingest_hierarchy` écrit ensuite dans `source_locator` (`page`/
    `page_end`), mais lu directement sur la hiérarchie pour ne pas dépendre
    d'un aller-retour DB juste après l'insertion."""
    plages: List[Tuple[int, int]] = []
    for node in hierarchy:
        page = node.get("page")
        if page is not None and str(node.get("content", "")).strip():
            plages.append((page, node.get("page_end") or page))
        enfants = node.get("children") or []
        if enfants:
            plages.extend(_plages_de_pages(enfants))
    return plages


def flag_structure_coverage_gaps(
    db: Session,
    document_id: uuid.UUID,
    markdown_text: str,
    hierarchy: List[Dict[str, Any]],
    run_id: Optional[uuid.UUID] = None,
    min_chars_par_page: int = 20,
) -> bool:
    """Émet un flag de curation NON bloquant quand une page porte du contenu
    markdown réel mais n'est couverte par AUCUN article/nœud de la
    hiérarchie parsée — texte perdu entre l'extraction et la structuration.
    Mesure 2 de mibeko-python#24 (§ 3.5 du plan « boîte de réception »),
    scopée à la plage propre à CE document comme la Mesure 1 (même raison :
    jamais comparer un acte au nombre total de pages du PDF source).

    Ne classe pas encore les pages non structurées en « annexe conservée »
    vs « écartée avec une raison » (ambition complète de la Mesure 2, § 3.5) :
    le parseur ne porte aucune trace des blocs qu'il a délibérément écartés
    (sommaire, mobilier de page) par opposition à ceux qu'il a simplement
    manqués — cette distinction resterait à construire dans le parseur
    lui-même, hors périmètre de ce lot (qui livre le changement de schéma et
    d'ancrage, § 3.5, sans le découpage physique des PDF par acte, différé à
    un ticket séparé). Un signalement ici dit seulement « du texte existe à
    cette page et n'apparaît dans aucun article » — déjà utile pour la revue.

    Idempotent : purge son propre flag non résolu avant de recalculer.
    """
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document_id,
        CurationFlag.source == "heuristic",
        CurationFlag.type_probleme == "bloc_non_structure",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)

    pages_content = _contenu_par_page(markdown_text)
    if not pages_content:
        return False

    pages_couvertes: set = set()
    for debut, fin in _plages_de_pages(hierarchy):
        pages_couvertes.update(range(debut, fin + 1))

    pages_non_structurees = sorted(
        page for page, contenu in pages_content.items()
        if len(contenu.strip()) >= min_chars_par_page and page not in pages_couvertes
    )

    if not pages_non_structurees:
        return False

    liste = ", ".join(str(p) for p in pages_non_structurees[:10])
    reste = f" (+{len(pages_non_structurees) - 10} autre(s))" if len(pages_non_structurees) > 10 else ""
    db.add(CurationFlag(
        document_id=document_id,
        source="heuristic",
        type_probleme="bloc_non_structure",
        severity="warning",
        description=(
            f"{len(pages_non_structurees)} page(s) portent du contenu qui n'apparaît dans "
            f"aucun article de ce document : {liste}{reste}. "
            "Vérifier si un bloc a été perdu entre l'extraction et la structuration."
        ),
    ))
    return True


def merge_metadata(document: LegalDocument, extra: dict) -> None:
    """Fusionne des metadonnees sans ecraser tout le bloc JSON existant."""

    current = document.metadata_ or {}
    document.metadata_ = {**current, **extra}
