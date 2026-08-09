"""Normalisation des tableaux produits par MinerU.

MinerU rend chaque tableau en HTML sur une seule ligne (``<table><tr><td>…``).
Stocké tel quel, ce balisage ressort en clair sur toutes les surfaces de
lecture, pollue le tsvector et les embeddings, et part dans le presse-papier
d'un utilisateur qui partage un article.

Invariant retenu (`docs/decisions.md`, 09/08/2026) : **``contenu_texte`` ne
contient jamais de balisage.** Un tableau y est *linéarisé* — une ligne par
rangée, cellules séparées par « | » — et sa forme canonique voyage à côté, dans
``article_versions.source_locator['tables']``, avec les bornes de lignes qu'il
occupe dans le texte. Une surface qui ignore cette structure affiche donc un
texte lisible ; celle qui la lit remplace ces lignes par un vrai tableau.

Doctrine de conversion, identique à celle du nettoyeur LaTeX (mibeko-python#11)
et non négociable sur un corpus juridique : **on convertit ce dont on est sûr,
on signale le reste, on ne devine jamais.** Un tableau dont la géométrie est
incertaine (``rowspan``, rangées de largeurs inégales) ou dont l'arithmétique ne
tombe pas juste est normalisé *et* accompagné d'anomalies destinées à un
`CurationFlag` — jamais corrigé en silence.

Jumeaux d'affichage (repli sur le balisage hérité, mêmes cas) :
``mibeko-site/src/lib/tables.ts``, ``mibeko-front/src/shared/lib/tables.ts``.

Exécutable sans base :  python3 tests/test_tables.py
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "LegalTable",
    "TableAnomaly",
    "CELL_SEPARATOR",
    "TABLE_LINE_PATTERN",
    "contains_table_markup",
    "parse_html_table",
    "linearize_table",
    "normalize_content",
    "looks_like_subscription_grid",
]

#: Séparateur de cellules dans la forme linéarisée. Doit rester identique aux
#: jumeaux TypeScript et Kotlin : c'est le contrat de lecture des surfaces.
CELL_SEPARATOR = " | "

#: Ligne de markdown MinerU entièrement occupée par un tableau HTML.
TABLE_LINE_PATTERN = re.compile(r"^<table[\s>]", re.IGNORECASE)

_TABLE_BLOCK = re.compile(r"<table\b[^>]*>(.*?)</table\s*>", re.IGNORECASE | re.DOTALL)
_ROW_BLOCK = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
_CELL_BLOCK = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_INNER_TAG = re.compile(r"<[^>]*>")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

#: Borne haute d'un ``colspan``. Au-delà, l'attribut est du bruit OCR et non une
#: intention de mise en forme : la cellule compte pour une, et le tableau est
#: signalé. Gonfler la rangée de milliers de cellules vides serait pire que le
#: mal. Les jumeaux TypeScript et Kotlin appliquent la même borne.
_MAX_COLSPAN = 32

#: Une cellule est « numérique » si elle ne porte qu'un nombre (formats du
#: corpus : « 37.000.000 », « 1 250 », « 12,5 », « 5% »). Sert au contrôle
#: arithmétique, jamais à réécrire la cellule.
_NUMERIC_CELL = re.compile(r"^[\d][\d\s.,]*$")


@dataclass
class TableAnomaly:
    """Anomalie relevée sur un tableau, destinée à un `CurationFlag`."""

    code: str
    message: str
    #: `blocking` empêche la publication ; `warning` informe l'éditeur.
    severity: str = "warning"


@dataclass
class LegalTable:
    """Tableau sous forme canonique."""

    headers: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    caption: Optional[str] = None
    #: Bornes des lignes occupées dans le `contenu_texte` (début inclus, fin exclue).
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    #: HTML MinerU d'origine — provenance et retraitement possible.
    html_source: Optional[str] = None

    @property
    def width(self) -> int:
        """Nombre de colonnes de référence (en-tête, sinon rangée la plus large)."""
        if self.headers:
            return len(self.headers)
        return max((len(row) for row in self.rows), default=0)

    def to_locator(self) -> Dict[str, Any]:
        """Forme stockée dans `source_locator['tables']`."""
        payload: Dict[str, Any] = {
            "caption": self.caption,
            "headers": self.headers,
            "rows": self.rows,
        }
        if self.line_start is not None:
            payload["line_start"] = self.line_start
        if self.line_end is not None:
            payload["line_end"] = self.line_end
        if self.html_source is not None:
            payload["html_source"] = self.html_source
        return payload


def contains_table_markup(text: Optional[str]) -> bool:
    """Vrai si le texte porte encore du balisage de tableau."""
    return bool(text) and bool(re.search(r"<table\b", text, re.IGNORECASE))


def _cell_text(fragment: str) -> str:
    """Texte d'une cellule : balises internes retirées, entités décodées."""
    return " ".join(html_module.unescape(_INNER_TAG.sub(" ", fragment)).split())


def _colspan(attributes: str) -> Tuple[int, bool]:
    """Portée horizontale d'une cellule, et si l'attribut lu était aberrant."""
    match = re.search(r"colspan\s*=\s*[\"']?(\d+)", attributes, re.IGNORECASE)
    if not match:
        return 1, False
    value = int(match.group(1))
    if value > _MAX_COLSPAN:
        return 1, True
    return max(value, 1), False


def _has_rowspan(attributes: str) -> bool:
    match = re.search(r"rowspan\s*=\s*[\"']?(\d+)", attributes, re.IGNORECASE)
    return bool(match) and int(match.group(1)) > 1


def _looks_like_header(cells: List[str]) -> bool:
    """Une rangée sert-elle d'en-tête ?

    MinerU n'émet pas de ``<th>`` : il faut trancher sur le contenu. Critère
    volontairement étroit — chaque cellule non vide doit contenir au moins une
    lettre. Une rangée de données du corpus commence presque toujours par un
    identifiant chiffré (« 3-2-1 ») ou un montant, ce qui la disqualifie.
    """
    filled = [cell for cell in cells if cell]
    return bool(filled) and all(_LETTER.search(cell) for cell in filled)


def _to_number(cell: str) -> Optional[float]:
    """Valeur numérique d'une cellule, ou None si ce n'en est pas une.

    Le corpus écrit les montants « 37.000.000 » (point = millier) et les
    décimales « 12,5 » (virgule). On ne tente rien d'autre : une cellule
    ambiguë ne participe pas au contrôle arithmétique.
    """
    text = cell.strip()
    if not _NUMERIC_CELL.match(text):
        return None
    text = text.replace(" ", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_html_table(html: str) -> Tuple[Optional[LegalTable], List[TableAnomaly]]:
    """Convertit un fragment ``<table>…</table>`` en forme canonique.

    Renvoie ``(None, anomalies)`` si le fragment ne contient aucune rangée
    exploitable — l'appelant doit alors conserver le texte d'origine plutôt que
    de le perdre.
    """
    anomalies: List[TableAnomaly] = []
    block = _TABLE_BLOCK.search(html or "")
    if not block:
        return None, [
            TableAnomaly(
                "tableau_non_ferme",
                "Balise <table> sans fermeture : le fragment est laissé tel quel, "
                "à reprendre depuis le PDF source.",
                "blocking",
            )
        ]

    rows: List[List[str]] = []
    rowspan_seen = False
    colspan_aberrant = False

    for row_match in _ROW_BLOCK.finditer(block.group(1)):
        cells: List[str] = []
        for cell_match in _CELL_BLOCK.finditer(row_match.group(1)):
            attributes, fragment = cell_match.group(2), cell_match.group(3)
            rowspan_seen = rowspan_seen or _has_rowspan(attributes)
            cells.append(_cell_text(fragment))
            # `colspan` est aplati en cellules vides : l'alignement des colonnes
            # est préservé sans que le modèle ait à porter la fusion.
            span, aberrant = _colspan(attributes)
            colspan_aberrant = colspan_aberrant or aberrant
            cells.extend([""] * (span - 1))
        if cells:
            rows.append(cells)

    if not rows:
        return None, [
            TableAnomaly(
                "tableau_vide",
                "Tableau sans aucune rangée exploitable : contenu probablement resté "
                "en image dans le PDF source.",
                "blocking",
            )
        ]

    if colspan_aberrant:
        anomalies.append(
            TableAnomaly(
                "tableau_colspan_aberrant",
                f"Attribut colspan supérieur à {_MAX_COLSPAN} : lu comme une cellule simple. "
                "La structure du tableau est douteuse — vérifier contre le PDF source.",
            )
        )

    if rowspan_seen:
        anomalies.append(
            TableAnomaly(
                "tableau_rowspan",
                "Cellules fusionnées verticalement (rowspan) : la forme à plat ne peut "
                "pas les représenter fidèlement. Vérifier le tableau contre le PDF source.",
            )
        )

    has_header = len(rows) > 1 and _looks_like_header(rows[0])
    table = LegalTable(
        headers=rows[0] if has_header else [],
        rows=rows[1:] if has_header else rows,
        html_source=block.group(0),
    )

    anomalies.extend(_check_geometry(table))
    anomalies.extend(_check_arithmetic(table))

    return table, anomalies


def _check_geometry(table: LegalTable) -> List[TableAnomaly]:
    """Rangées de largeurs inégales, tableau entièrement vide."""
    anomalies: List[TableAnomaly] = []
    widths = {len(row) for row in table.rows}

    if len(widths) > 1:
        anomalies.append(
            TableAnomaly(
                "tableau_largeur_irreguliere",
                "Rangées de largeurs inégales ("
                + ", ".join(f"{width} cellule(s)" for width in sorted(widths))
                + ") : une cellule a probablement été perdue à l'OCR.",
            )
        )
    elif table.headers and widths and table.width not in widths:
        anomalies.append(
            TableAnomaly(
                "tableau_largeur_irreguliere",
                f"En-tête à {len(table.headers)} colonne(s) mais rangées à "
                f"{widths.pop()} : colonne manquante ou en-tête mal détecté.",
            )
        )

    if not any(cell for row in table.rows for cell in row):
        anomalies.append(
            TableAnomaly(
                "tableau_vide",
                "Toutes les cellules de données sont vides : le tableau n'a pas été extrait.",
                "blocking",
            )
        )

    return anomalies


def _amount(value: float) -> str:
    """Montant en notation française, pour les messages d'anomalie."""
    return f"{value:,.0f}".replace(",", " ")


def _equal(actual: float, expected: float) -> bool:
    """Égalité à un arrondi flottant près.

    Le corpus n'écrit pas de décimales sur les montants : toute différence
    réelle vaut au moins 1.
    """
    return abs(actual - expected) <= max(1.0, abs(expected) * 1e-9)


def _numeric_grid(table: LegalTable) -> Tuple[List[List[Optional[float]]], List[int]]:
    """Grille des valeurs numériques et colonnes exploitables du tableau.

    Une colonne est retenue si elle porte un nombre sur au moins 80 % des
    rangées : les colonnes de libellés (« Assemblée législative (personnel) »)
    et d'identifiants (« 3-2-1 ») en sont écartées, tandis qu'une poignée de
    cellules illisibles ne disqualifie pas une colonne de montants.
    """
    width = table.width
    grid: List[List[Optional[float]]] = []
    for row in table.rows:
        values = [_to_number(cell) for cell in row[:width]]
        values.extend([None] * (width - len(values)))
        grid.append(values)

    if not grid:
        return [], []

    numeric_columns = [
        column
        for column in range(width)
        if sum(1 for row in grid if row[column] is not None) >= 0.8 * len(grid)
    ]
    return grid, numeric_columns


def _check_row_sums(
    grid: List[List[Optional[float]]],
    numeric_columns: List[int],
) -> List[TableAnomaly]:
    """Une colonne est-elle la somme des autres, rangée par rangée ?

    Cas type du corpus : « crédits primitifs + crédits supplémentaires =
    crédits nouveaux ». Quand la relation tient sur la quasi-totalité des
    rangées mais achoppe sur quelques-unes, ce sont exactement celles-là qui ont
    été mal océrisées : on les nomme, plutôt que d'obliger un éditeur à relire
    tout le tableau contre le PDF.
    """
    if len(numeric_columns) < 3:
        return []

    for target in reversed(numeric_columns):
        sources = [column for column in numeric_columns if column != target]
        matches = 0
        mismatches: List[Tuple[int, float, float]] = []

        for index, row in enumerate(grid):
            if row[target] is None or any(row[column] is None for column in sources):
                continue
            expected = sum(row[column] for column in sources)  # type: ignore[misc]
            if _equal(row[target], expected):  # type: ignore[arg-type]
                matches += 1
            else:
                mismatches.append((index, row[target], expected))  # type: ignore[arg-type]

        # La relation doit être manifestement la règle du tableau (au moins
        # trois rangées justes, et deux fois plus de justes que de fausses)
        # avant qu'un écart vaille signalement.
        if matches >= 3 and mismatches and matches >= 2 * len(mismatches):
            detail = " ; ".join(
                f"rangée {index + 1} : {_amount(actual)} au lieu de {_amount(expected)}"
                for index, actual, expected in mismatches[:5]
            )
            return [
                TableAnomaly(
                    "tableau_somme_incoherente",
                    f"La colonne {target + 1} est la somme des autres sur {matches} rangée(s) "
                    f"mais pas sur {len(mismatches)} — {detail}. "
                    "Chiffre probablement mal océrisé : vérifier ces rangées contre le PDF source.",
                )
            ]

    return []


def _check_column_totals(
    grid: List[List[Optional[float]]],
    numeric_columns: List[int],
) -> List[TableAnomaly]:
    """La dernière rangée est-elle une rangée de totaux, et tombe-t-elle juste ?

    Une rangée de totaux se reconnaît à ceci qu'elle est *proche* de la somme
    des rangées au-dessus. Proche sans l'égaler : une cellule du tableau a été
    mal lue, et l'écart chiffre l'erreur. Loin : ce n'est pas une rangée de
    totaux, et on se tait.
    """
    if not numeric_columns or len(grid) < 4:
        return []

    last, body = grid[-1], grid[:-1]
    concordantes = 0
    gaps: List[Tuple[int, float, float]] = []

    for column in numeric_columns:
        announced = last[column]
        if announced is None or announced == 0:
            continue
        total = sum(value for row in body if (value := row[column]) is not None)
        if _equal(announced, total):
            concordantes += 1
        elif abs(announced - total) <= abs(announced) * 0.01:
            concordantes += 1
            gaps.append((column, announced, total))

    # Au moins deux colonnes doivent se comporter en totaux : une seule
    # coïncidence à 1 % près serait du hasard, pas une rangée de totaux.
    if concordantes < 2 or not gaps:
        return []

    detail = " ; ".join(
        f"colonne {column + 1} : {_amount(announced)} annoncé, {_amount(total)} calculé "
        f"(écart {_amount(announced - total)})"
        for column, announced, total in gaps
    )
    return [
        TableAnomaly(
            "tableau_total_incoherent",
            f"La dernière rangée totalise les précédentes ({concordantes} colonne(s) "
            f"concordantes sur {len(numeric_columns)}) mais {len(gaps)} n'y tombe(nt) pas "
            f"juste — {detail}. Une cellule du tableau a probablement été mal océrisée : "
            "vérifier contre le PDF source.",
        )
    ]


def _check_arithmetic(table: LegalTable) -> List[TableAnomaly]:
    """Contrôles arithmétiques : somme en ligne, puis rangée de totaux.

    Les deux relations sont indépendantes et se complètent — sur le décret
    budgétaire de 1959, la première désigne la rangée fautive et la seconde
    chiffre l'écart qu'elle provoque sur le total du budget.
    """
    if table.width < 3 or len(table.rows) < 3:
        return []

    grid, numeric_columns = _numeric_grid(table)
    if not numeric_columns:
        return []

    return _check_row_sums(grid, numeric_columns) + _check_column_totals(grid, numeric_columns)


def linearize_table(table: LegalTable) -> str:
    """Rendu textuel : caption, en-tête, puis une ligne par rangée."""
    lines: List[str] = []
    if table.caption:
        lines.append(table.caption)
    if table.headers:
        lines.append(CELL_SEPARATOR.join(table.headers))
    lines.extend(CELL_SEPARATOR.join(row) for row in table.rows)
    return "\n".join(lines)


def normalize_content(
    content: str,
    caption: Optional[str] = None,
) -> Tuple[str, List[LegalTable], List[TableAnomaly]]:
    """Remplace tout balisage de tableau d'un contenu par sa forme linéarisée.

    Renvoie le texte normalisé (sans balise), les tableaux canoniques ancrés sur
    leurs lignes, et les anomalies relevées. Le texte hors tableau est conservé
    tel quel : un acte peut porter une phrase d'introduction suivie d'un tableau
    de coordonnées.

    Idempotent : un contenu déjà normalisé ressort inchangé, sans tableau — ce
    qui rend le rattrapage du corpus rejouable sans risque de double conversion.
    """
    if not contains_table_markup(content):
        return content, [], []

    pieces: List[str] = []
    tables: List[LegalTable] = []
    anomalies: List[TableAnomaly] = []
    cursor = 0

    for block in _TABLE_BLOCK.finditer(content):
        pieces.append(content[cursor : block.start()])
        table, block_anomalies = parse_html_table(block.group(0))
        anomalies.extend(block_anomalies)

        if table is None:
            # Rien d'exploitable : on garde le fragment d'origine. Un texte
            # officiel illisible vaut mieux qu'un texte officiel amputé.
            pieces.append(block.group(0))
        else:
            table.caption = caption if len(tables) == 0 else None
            tables.append(table)
            pieces.append(linearize_table(table))

        cursor = block.end()

    pieces.append(content[cursor:])

    # Balise ouvrante jamais refermée : `_TABLE_BLOCK` ne l'a pas vue, le texte
    # est donc passé intact — on le signale sans y toucher.
    normalized = "".join(pieces).strip()
    if contains_table_markup(normalized):
        anomalies.append(
            TableAnomaly(
                "tableau_non_ferme",
                "Balisage de tableau non refermé, laissé tel quel : à reprendre "
                "depuis le PDF source.",
                "blocking",
            )
        )

    _anchor_tables(normalized, tables)
    return normalized, tables, anomalies


def _anchor_tables(normalized: str, tables: List[LegalTable]) -> None:
    """Renseigne les bornes de lignes de chaque tableau dans le texte normalisé.

    L'ancrage est cherché à partir de la fin du tableau précédent : deux
    tableaux identiques dans un même article restent distincts.
    """
    lines = normalized.split("\n")
    cursor = 0

    for table in tables:
        block = linearize_table(table).split("\n")
        if not block:
            continue
        for start in range(cursor, len(lines) - len(block) + 1):
            if lines[start : start + len(block)] == block:
                table.line_start = start
                table.line_end = start + len(block)
                cursor = table.line_end
                break


#: Marqueurs d'une grille tarifaire d'abonnement au Journal officiel — l'« ours »
#: que MinerU capture en tête de JO et que l'ingestion prend pour un article.
#: Écrits sans accent ni espace : l'OCR des JO anciens éclate les mots
#: (« ABON NÉM EN T S » relevé sur le JO n° 29-1963), et un motif littéral les
#: manquerait tous.
_SUBSCRIPTION_PERIODS = ("1an", "6mois", "3mois", "abonnement", "abonnements")
_SUBSCRIPTION_HINTS = _SUBSCRIPTION_PERIODS + (
    "destination",
    "annonce",
    "numero",
    "tarif",
    "voieaerienne",
    "etranger",
)

_ACCENTS = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")


def _ocr_haystack(table: LegalTable) -> str:
    """Cellules du tableau réduites à une forme comparable malgré l'OCR."""
    brut = " ".join(table.headers + [cell for row in table.rows for cell in row])
    return "".join(brut.lower().translate(_ACCENTS).split())


def looks_like_subscription_grid(table: LegalTable) -> bool:
    """Vrai si le tableau est la grille d'abonnements d'un JO, pas du droit.

    Ce n'est pas un défaut de conversion mais un **faux article** : le tableau
    ne devrait pas exister en base. La détection sert à le proposer au retrait
    (cf. `RetirerArticlesMastheadCommand` côté Laravel), jamais à supprimer
    d'office — la suppression reste une décision humaine, et c'est pourquoi le
    critère peut se permettre d'être large.

    Une durée d'abonnement est exigée en plus du compte de marqueurs : un
    tableau juridique peut parler de « destinations » et de « tarifs » (droits
    de douane, transport), rarement en regard d'un « 1 AN / 6 MOIS ».
    """
    haystack = _ocr_haystack(table)
    hits = sum(1 for hint in _SUBSCRIPTION_HINTS if hint in haystack)
    has_period = any(period in haystack for period in _SUBSCRIPTION_PERIODS)
    return hits >= 3 and has_period
