"""Tests de la structuration jurisprudence CCJA (mibeko-python#19).

`html_to_text`/`parse_arret_html`/`extract_citations` sont des fonctions
pures : testées directement, sans base. `structure_arret` est testée avec
une session fake (aucun réseau, aucune Postgres réelle), comme
`test_structuration_structurer.py` — la résolution d'un Acte uniforme réel
(`resolve_au_article`) est en revanche testée contre la base de
développement réelle (flush + rollback, jamais de commit) : c'est une
requête SQL véritable (ILIKE/préfixe) que mocker rendrait creuse.
"""

import uuid

import pytest

from src.acquisition.manifest import ManifestEntry
from src.db.database import SessionLocal
from src.db.models import Article, ArticleVersion, JurisprudenceCitation, LegalDocument
from src.structuration.jurisprudence import (
    extract_citations,
    html_to_text,
    parse_arret_html,
    resolve_au_article,
    structure_arret,
)

ARRET_HTML = """<html><body><article>
ORGANISATION POUR L'HARMONISATION EN AFRIQUE DU DROIT DES AFFAIRES (OHADA)
COUR COMMUNE DE JUSTICE ET D'ARBITRAGE (CCJA)
Première chambre - Audience publique du 13 juillet 2023
Affaire : B AG (Conseil : Maître X, Avocat à la Cour)
Contre Société Générale de Cote d'Ivoire, dite SGCI, SA (Conseils : Cabinet Y, Avocats à la Cour)
Arrêt N° 163/2023 du 13 juillet 2023
La Cour Commune de Justice et d'Arbitrage a statué comme suit :
Attendu qu'il est fait grief à l'arrêt d'avoir violé l'article 301 de l'Acte uniforme portant sur le
droit commercial général en ce qu'il a mal interprété la convention ;
Attendu également la violation des articles 170 et 178 du Code de procédure civile ivoirien ;
PAR CES MOTIFS Rejette le pourvoi.
</article></body></html>"""


def _entry(**overrides) -> ManifestEntry:
    base = dict(
        id="juricaf/OHADA-TEST-20230713-1632023",
        fichier="sources/juricaf/OHADA-TEST-20230713-1632023.html",
        sha256="a" * 64,
        size_bytes=100,
        type_source="jurisprudence_ccja",
        source_url="https://juricaf.org/arret/OHADA-TEST-20230713-1632023",
        fetched_at="2026-09-06T00:00:00+00:00",
        statut="telecharge",
    )
    base.update(overrides)
    return ManifestEntry(**base)


def test_html_to_text_repare_une_entite_utf8_coupee_par_une_balise():
    # `<p>` inséré au beau milieu des 3 octets UTF-8 de l'apostrophe
    # typographique ’ (0xE2 0x80 0x99) — constaté en réel sur juricaf.org
    # le 06/09/2026. Retirer la balise AVANT de décoder répare la séquence.
    raw = b"<article>Cote d\xe2<p>\x80\x99Ivoire</article>"
    assert html_to_text(raw) == "Cote d’Ivoire"


def test_html_to_text_retire_script_et_style_avec_leur_contenu():
    raw = b"<article><style>.x{color:red}</style>Texte utile<script>alert(1)</script></article>"
    assert html_to_text(raw) == "Texte utile"


def test_html_to_text_se_limite_a_la_balise_article_quand_presente():
    raw = b"<nav>menu du site</nav><article>Contenu de la decision</article><footer>pied de page</footer>"
    assert html_to_text(raw) == "Contenu de la decision"


def test_parse_arret_html_extrait_numero_date_et_chambre():
    parsed = parse_arret_html(ARRET_HTML.encode("utf-8"))

    assert parsed.numero == "163/2023"
    assert parsed.date_decision.isoformat() == "2023-07-13"
    assert parsed.chambre == "Première chambre"
    assert "PAR CES MOTIFS" in parsed.texte_integral


def test_extract_citations_distingue_acte_uniforme_et_code_national():
    parsed = parse_arret_html(ARRET_HTML.encode("utf-8"))
    citations = extract_citations(parsed.texte_integral)

    au = next(c for c in citations if c.numero_article == "301")
    assert au.acte_libelle.startswith("portant sur le droit commercial général")

    hors_corpus = next(c for c in citations if c.numero_article is None)
    assert "Code de procédure civile" in hors_corpus.reference_brute


def test_extract_citations_ne_duplique_pas_une_reference_repetee():
    texte = "l'article 5 de l'Acte uniforme portant sur le droit commercial général. " * 2
    citations = extract_citations(texte)
    assert len(citations) == 1


class FakeQuery:
    def __init__(self, first_result=None, all_result=None):
        self._first = first_result
        self._all = all_result or []

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._first

    def all(self):
        return list(self._all)


class FakeSession:
    """Miroir de `FakeSession` dans test_structuration_structurer.py.

    `resolve_au_article` interrogera `.all()` sur `LegalDocument` : vide par
    défaut, donc toute citation d'Acte uniforme reste `cited_article_id=None`
    dans ces tests d'orchestration — la résolution réelle est testée à part,
    contre la base de développement.
    """

    def __init__(self, existing_document=None):
        self.existing_document = existing_document
        self.added = []
        self.committed = False
        self.rolled_back = False

    def query(self, model):
        if model is LegalDocument:
            return FakeQuery(first_result=self.existing_document, all_result=[])
        return FakeQuery()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_structure_arret_cree_document_article_version_et_citations(tmp_path):
    (tmp_path / "sources" / "juricaf").mkdir(parents=True)
    (tmp_path / "sources" / "juricaf" / "OHADA-TEST-20230713-1632023.html").write_text(ARRET_HTML, encoding="utf-8")

    db = FakeSession()
    result = structure_arret(db, _entry(), tmp_path)

    assert result["statut"] == "structure"
    assert result["citations"] == 2
    assert db.committed is True

    document = next(o for o in db.added if isinstance(o, LegalDocument))
    assert document.titre_officiel == "Arrêt CCJA n° 163/2023 du 13/07/2023"
    assert document.type_code == "JURIS"
    assert document.legal_scope == "ohada"
    assert document.curation_status == "draft"
    assert document.libelle_descriptif == "B AG c/ Société Générale de Cote d'Ivoire, dite SGCI, SA"
    assert document.libelle_descriptif_source == "article"

    article = next(o for o in db.added if isinstance(o, Article))
    assert article.numero_article == "163/2023"

    version = next(o for o in db.added if isinstance(o, ArticleVersion))
    assert "PAR CES MOTIFS" in version.contenu_texte

    citations = [o for o in db.added if isinstance(o, JurisprudenceCitation)]
    assert len(citations) == 2
    assert all(c.decision_id == document.id for c in citations)
    # Aucun Acte uniforme connu de cette session fake : aucune résolution.
    assert all(c.cited_article_id is None for c in citations)


def test_structure_arret_est_idempotent_par_document_key(tmp_path):
    (tmp_path / "sources" / "juricaf").mkdir(parents=True)
    (tmp_path / "sources" / "juricaf" / "OHADA-TEST-20230713-1632023.html").write_text(ARRET_HTML, encoding="utf-8")

    existing_id = uuid.uuid4()
    existing = LegalDocument(id=existing_id, titre_officiel="Arrêt CCJA n° 163/2023 du 13/07/2023")
    db = FakeSession(existing_document=existing)

    result = structure_arret(db, _entry(), tmp_path)

    assert result == {"statut": "deja_existant", "document_id": existing_id, "motif": None}
    assert db.added == []
    assert db.committed is False


def test_structure_arret_html_sans_numero_est_une_erreur_tracee(tmp_path):
    (tmp_path / "sources" / "juricaf").mkdir(parents=True)
    (tmp_path / "sources" / "juricaf" / "OHADA-TEST-20230713-1632023.html").write_text(
        "<article>Texte sans numéro d'arrêt reconnaissable.</article>", encoding="utf-8"
    )

    db = FakeSession()
    result = structure_arret(db, _entry(), tmp_path)

    assert result["statut"] == "erreur"
    assert db.added == []
    assert db.committed is False


def test_structure_arret_fichier_html_introuvable_est_une_erreur_tracee(tmp_path):
    db = FakeSession()
    result = structure_arret(db, _entry(fichier="sources/juricaf/absent.html"), tmp_path)

    assert result["statut"] == "erreur"
    assert "lecture du HTML" in result["motif"]


@pytest.fixture()
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()  # jamais de commit : aucune trace dans la base de dev
        session.close()


def test_resolve_au_article_trouve_un_acte_uniforme_reel_du_corpus(db_session):
    # Libellé fictif et unique (suffixe aléatoire) : la base de dev contient
    # déjà un vrai « Acte uniforme portant sur le droit commercial général »
    # (article 301 y compris) — un titre réaliste ferait ambiguë la
    # résolution entre le vrai document du corpus et ce fixture, `max()`
    # départageant alors arbitrairement plutôt que d'échouer franchement.
    domaine_test = f"un domaine de test {uuid.uuid4().hex[:8]}"
    acte = LegalDocument(
        titre_officiel=f"Acte uniforme portant sur {domaine_test} (révisé)",
        document_key=f"flux:test-jurisprudence-{uuid.uuid4()}",
        document_role="FLUX",
        type_code="AU",
        legal_scope="ohada",
        curation_status="draft",
    )
    db_session.add(acte)
    db_session.flush()
    article = Article(document_id=acte.id, numero_article="301", ordre_affichage=0)
    db_session.add(article)
    db_session.flush()

    # Le libellé extrait déborde toujours sur la prose qui suit, en réel.
    libelle_deborde = f"portant sur {domaine_test} en ce qu'il a mal interprété la convention"

    resolved = resolve_au_article(db_session, libelle_deborde, "301")

    assert resolved == article.id


def test_resolve_au_article_sans_correspondance_renvoie_none(db_session):
    resolved = resolve_au_article(db_session, "portant sur un acte totalement inventé", "999")
    assert resolved is None
