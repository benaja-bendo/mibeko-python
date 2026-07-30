"""Garde-fous et logique de plan du push additif dev → production.

Aucun test ici n'ouvre de connexion : la logique de plan (dédoublonnage par
provenance, collisions d'unicité, clôture des journaux officiels) est une fonction
pure, et les constructeurs de connexions doivent refuser toute configuration
ambiguë avant même de tenter un réseau.
"""

import pytest

from src.promotion.push_corpus import (
    CibleProdAmbigue,
    ConfigurationProdManquante,
    DocumentSource,
    EtatCible,
    JournalSource,
    _expression_selection,
    charger_cible_ecriture,
    construire_plan,
    creer_client_minio_ecriture,
    creer_client_minio_source,
    creer_engine_source,
)


def _doc(**surcharge) -> DocumentSource:
    """Document source minimal, sans collision par défaut."""
    base = dict(
        id="00000000-0000-0000-0000-000000000001",
        titre="Loi de test",
        slug=None,
        document_key=None,
        stock_code=None,
        reference_nor=None,
        official_journal_id=None,
        checksums_sources=frozenset({"cafe" * 16}),
    )
    base.update(surcharge)
    return DocumentSource(**base)


def _cible(**surcharge) -> EtatCible:
    """Cible vide : rien n'entre en collision par défaut."""
    base = dict(
        ids_documents=frozenset(),
        checksums_sources=frozenset(),
        slugs=frozenset(),
        document_keys=frozenset(),
        stock_codes=frozenset(),
        references_nor=frozenset(),
        ids_journaux=frozenset(),
        journaux_par_date_numero={},
    )
    base.update(surcharge)
    return EtatCible(**base)


def test_document_neuf_est_pousse():
    plan = construire_plan([_doc()], [], _cible())

    assert len(plan.a_pousser) == 1
    assert plan.ecartes == []


def test_document_deja_pousse_est_ecarte_en_premier():
    """L'idempotence prime : un id déjà en cible n'est examiné pour rien d'autre."""
    doc = _doc()
    plan = construire_plan(
        [doc],
        [],
        _cible(
            ids_documents=frozenset({doc.id}),
            checksums_sources=doc.checksums_sources,
        ),
    )

    assert plan.a_pousser == []
    [(_, motif)] = plan.ecartes
    assert "déjà poussé" in motif


def test_source_deja_en_production_est_ecartee():
    """Même texte, autre forme locale : la version de la production fait foi."""
    plan = construire_plan(
        [_doc()], [], _cible(checksums_sources=frozenset({"cafe" * 16}))
    )

    assert plan.a_pousser == []
    [(_, motif)] = plan.ecartes
    assert "source déjà en production" in motif


@pytest.mark.parametrize(
    ("champ", "valeur", "cle_cible"),
    [
        ("slug", "code-du-travail", "slugs"),
        ("document_key", "flux:loi-1", "document_keys"),
        ("stock_code", "code-penal", "stock_codes"),
        ("reference_nor", "16/2017", "references_nor"),
    ],
)
def test_collision_d_unicite_ecarte_le_document(champ, valeur, cle_cible):
    """Pousser quand même échouerait sur l'index partiel ; on écarte en le disant."""
    plan = construire_plan(
        [_doc(**{champ: valeur})], [], _cible(**{cle_cible: frozenset({valeur})})
    )

    assert plan.a_pousser == []
    [(_, motif)] = plan.ecartes
    assert valeur in motif


def test_journal_requis_est_cree_une_seule_fois():
    jo = JournalSource(id="jo-1", publication_date="2026-01-15", number="2026-03")
    docs = [
        _doc(id=f"00000000-0000-0000-0000-00000000000{i}",
             checksums_sources=frozenset({f"beef{i}" * 12 + "beef"}),
             official_journal_id="jo-1")
        for i in (1, 2)
    ]

    plan = construire_plan(docs, [jo], _cible())

    assert len(plan.a_pousser) == 2
    assert plan.journaux_a_creer == [jo]
    assert plan.remap_journaux == {}


def test_journal_deja_en_cible_par_id_n_est_pas_recree():
    jo = JournalSource(id="jo-1", publication_date="2026-01-15", number="2026-03")
    plan = construire_plan(
        [_doc(official_journal_id="jo-1")],
        [jo],
        _cible(ids_journaux=frozenset({"jo-1"})),
    )

    assert plan.journaux_a_creer == []
    assert plan.remap_journaux == {}


def test_journal_homonyme_en_cible_est_remappe():
    """Même (date, numéro) sous un autre id : on rattache à la fiche existante
    plutôt que de violer uq_official_journals_pubdate_number."""
    jo = JournalSource(id="jo-src", publication_date="2026-01-15", number="2026-03")
    plan = construire_plan(
        [_doc(official_journal_id="jo-src")],
        [jo],
        _cible(journaux_par_date_numero={("2026-01-15", "2026-03"): "jo-cible"}),
    )

    assert plan.journaux_a_creer == []
    assert plan.remap_journaux == {"jo-src": "jo-cible"}


def test_journal_orphelin_interrompt_le_plan():
    """Une FK vers un journal absent est une anomalie de la source, pas un cas à
    rattraper silencieusement."""
    with pytest.raises(ValueError):
        construire_plan([_doc(official_journal_id="jo-fantome")], [], _cible())


def test_journal_d_un_document_ecarte_n_est_pas_cree():
    """Seuls les documents poussés tirent leurs journaux avec eux."""
    jo = JournalSource(id="jo-1", publication_date="2026-01-15", number="2026-03")
    doc = _doc(official_journal_id="jo-1")
    plan = construire_plan(
        [doc], [jo], _cible(checksums_sources=doc.checksums_sources)
    )

    assert plan.a_pousser == []
    assert plan.journaux_a_creer == []


# ---------------------------------------------------------------------------
# Réécritures de colonnes
# ---------------------------------------------------------------------------


def test_curation_status_est_force_a_draft():
    """Un document publié en dev arrive en staging : la publication se décide en
    production, via l'API Laravel."""
    expr = _expression_selection(
        "legal_documents", ["id", "curation_status"], {}
    )

    assert "'draft' as curation_status" in expr


def test_remap_journaux_reecrit_la_fk():
    expr = _expression_selection(
        "legal_documents", ["official_journal_id"], {"jo-src": "jo-cible"}
    )

    assert "case official_journal_id" in expr
    assert "'jo-src'::uuid then 'jo-cible'::uuid" in expr


def test_les_autres_tables_ne_sont_pas_reecrites():
    assert _expression_selection("articles", ["id", "numero_article"], {}) == (
        "id, numero_article"
    )


# ---------------------------------------------------------------------------
# Garde-fous des connexions (aucun réseau : le refus précède toute tentative)
# ---------------------------------------------------------------------------


def test_source_db_refusee_hors_port_dev(monkeypatch):
    monkeypatch.setenv("DB_PORT", "5434")

    with pytest.raises(CibleProdAmbigue):
        creer_engine_source()


def test_ecriture_refusee_sans_variables(monkeypatch):
    for var in ("PROD_RW_DB_HOST", "PROD_RW_DB_PORT", "PROD_RW_DB_DATABASE",
                "PROD_RW_DB_USERNAME", "PROD_RW_DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigurationProdManquante) as exc:
        charger_cible_ecriture()

    assert "JAMAIS" in str(exc.value)


def test_ecriture_refusee_sur_le_port_du_dev(monkeypatch):
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("PROD_RW_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("PROD_RW_DB_PORT", "5433")
    monkeypatch.setenv("PROD_RW_DB_DATABASE", "mibeko-db")
    monkeypatch.setenv("PROD_RW_DB_USERNAME", "pguser")
    monkeypatch.setenv("PROD_RW_DB_PASSWORD", "secret")

    with pytest.raises(CibleProdAmbigue):
        charger_cible_ecriture()


def test_minio_source_refuse_hors_port_dev(monkeypatch):
    monkeypatch.setenv("MINIO_PORT", "9100")

    with pytest.raises(CibleProdAmbigue):
        creer_client_minio_source()


def test_minio_ecriture_refuse_le_port_du_dev(monkeypatch):
    monkeypatch.setenv("PROD_RW_MINIO_ENDPOINT", "127.0.0.1:9000")
    monkeypatch.setenv("PROD_RW_MINIO_ACCESS_KEY", "cle")
    monkeypatch.setenv("PROD_RW_MINIO_SECRET_KEY", "secret")

    with pytest.raises(CibleProdAmbigue):
        creer_client_minio_ecriture()


def test_minio_ecriture_exige_ses_variables(monkeypatch):
    for var in ("PROD_RW_MINIO_ENDPOINT", "PROD_RW_MINIO_ACCESS_KEY",
                "PROD_RW_MINIO_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigurationProdManquante):
        creer_client_minio_ecriture()


def test_l_import_ne_cree_ni_engine_ni_client():
    """Comme prod_readonly : importer le module de push ne touche aucun service."""
    import subprocess
    import sys
    from pathlib import Path

    verification = (
        "import sys\n"
        "import src.promotion.push_corpus as m\n"
        "interdits = [n for n in ('src.db.database', 'src.services.minio_service')"
        " if n in sys.modules]\n"
        "assert not interdits, 'imports à effet de bord : %s' % interdits\n"
        "print('ok')\n"
    )
    racine = Path(__file__).resolve().parent.parent
    resultat = subprocess.run(
        [sys.executable, "-c", verification], cwd=racine,
        capture_output=True, text=True,
    )

    assert resultat.returncode == 0, resultat.stdout + resultat.stderr
