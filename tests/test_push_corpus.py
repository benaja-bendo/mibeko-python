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
    PlanInattendu,
    _expression_selection,
    charger_cible_ecriture,
    construire_plan,
    creer_client_minio_ecriture,
    creer_client_minio_source,
    creer_engine_source,
    filtrer_par_document_keys,
    limiter_plan,
    verifier_attendu,
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
        institutions_par_sigle={},
    )
    base.update(surcharge)
    return EtatCible(**base)


def test_document_neuf_est_pousse():
    plan = construire_plan([_doc()], [], _cible())

    assert len(plan.a_pousser) == 1
    assert plan.ecartes == []


def test_filtrer_par_document_keys_reduit_a_la_selection():
    voulu = _doc(document_key="loi-voulue")
    autre = _doc(id="00000000-0000-0000-0000-000000000002", document_key="autre-loi")

    filtres, manquants = filtrer_par_document_keys([voulu, autre], ["loi-voulue"])

    assert filtres == [voulu]
    assert manquants == set()


def test_filtrer_par_document_keys_signale_les_cles_absentes():
    doc = _doc(document_key="loi-voulue")

    filtres, manquants = filtrer_par_document_keys([doc], ["loi-voulue", "loi-inconnue"])

    assert filtres == [doc]
    assert manquants == {"loi-inconnue"}


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
    """Pousser quand même échouerait sur l'index unique (total pour le slug,
    partiels pour les trois autres clés) ; on écarte en le disant."""
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
# --limit : les journaux suivent les documents (mibeko-python#38)
# ---------------------------------------------------------------------------


def _deux_documents_sur_deux_journaux():
    jo_1 = JournalSource(id="jo-1", publication_date="2026-01-15", number="1")
    jo_2 = JournalSource(id="jo-2", publication_date="2026-02-15", number="2")
    docs = [
        _doc(id=f"00000000-0000-0000-0000-00000000000{i}",
             checksums_sources=frozenset({f"beef{i}" * 12 + "beef"}),
             official_journal_id=f"jo-{i}")
        for i in (1, 2)
    ]
    return docs, [jo_1, jo_2]


def test_limite_ne_cree_que_les_journaux_des_documents_pousses():
    """Constat du 07/08/2026 : `--limit 100` a créé les 39 fiches du plan entier,
    dont 24 sont restées publiées sans aucun texte."""
    docs, journaux = _deux_documents_sur_deux_journaux()
    plan = limiter_plan(construire_plan(docs, journaux, _cible()), 1)

    assert [d.id for d in plan.a_pousser] == [docs[0].id]
    assert plan.journaux_a_creer == [journaux[0]]


def test_limite_restreint_le_rattachement_aux_documents_pousses():
    docs, journaux = _deux_documents_sur_deux_journaux()
    cible = _cible(journaux_par_date_numero={
        ("2026-01-15", "1"): "jo-cible-1",
        ("2026-02-15", "2"): "jo-cible-2",
    })
    plan = limiter_plan(construire_plan(docs, journaux, cible), 1)

    assert plan.journaux_a_creer == []
    assert plan.remap_journaux == {"jo-1": "jo-cible-1"}


@pytest.mark.parametrize("limite", [None, 0])
def test_sans_limite_le_plan_est_rendu_tel_quel(limite):
    docs, journaux = _deux_documents_sur_deux_journaux()
    plan = construire_plan(docs, journaux, _cible())

    assert limiter_plan(plan, limite) is plan


def test_executer_push_avec_limite_ne_copie_que_le_journal_requis(monkeypatch):
    """Le chemin d'écriture lui-même, sans réseau : connexions factices,
    copie de table interceptée."""
    from unittest.mock import MagicMock

    import src.promotion.push_corpus as push_corpus

    copies = []

    def copier_table_espion(cnx_src, cnx_cbl, table, where, params, *remaps):
        copies.append((table, params))
        return 0

    monkeypatch.setattr(push_corpus, "copier_table", copier_table_espion)
    docs, journaux = _deux_documents_sur_deux_journaux()
    plan = construire_plan(docs, journaux, _cible())

    rapport = push_corpus.executer_push(
        MagicMock(), MagicMock(), plan, dry_run=False, limite=1
    )

    journaux_copies = [p["ids"] for table, p in copies if table == "official_journals"]
    assert journaux_copies == [["jo-1"]]
    assert rapport["journaux_a_creer"] == 1
    assert [d["id"] for d in rapport["documents"]] == [docs[0].id]


# ---------------------------------------------------------------------------
# --attendu : le nombre annoncé doit être celui du plan (PY-011, mibeko-python#39)
# ---------------------------------------------------------------------------


def _documents(n: int, depart: int = 0) -> list:
    """`n` documents distincts, sans collision ni source partagée."""
    return [
        _doc(id=f"00000000-0000-0000-0000-{i:012d}",
             document_key=f"flux:acte-{i}",
             checksums_sources=frozenset({f"sha-{i}"}))
        for i in range(depart, depart + n)
    ]


def test_attendu_conforme_laisse_passer():
    plan = construire_plan(_documents(3), [], _cible())

    assert verifier_attendu(plan, 3) is None


def test_attendu_conforme_apres_limite_compte_le_plan_limite():
    """Le chiffre qui compte est celui de `limiter_plan`, pas du plan complet."""
    plan = limiter_plan(construire_plan(_documents(3), [], _cible()), 2)

    assert verifier_attendu(plan, 2) is None
    with pytest.raises(PlanInattendu, match="pousserait 2 documents"):
        verifier_attendu(plan, 3)


def test_plan_trop_gros_est_refuse_et_liste_le_plan():
    """Sans --document-key le code n'a que le nombre : il liste le plan pour
    qu'on y reconnaisse l'intrus."""
    plan = construire_plan(_documents(3), [], _cible())

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 2)

    message = str(exc.value)
    assert "pousserait 3 documents" in message
    assert "annonce 2" in message
    assert "1 de trop" in message
    assert "Aucun --document-key" in message
    for i in range(3):
        assert f"flux:acte-{i}" in message


def test_plan_trop_petit_est_refuse():
    plan = construire_plan(_documents(2), [], _cible())

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 3)

    assert "pousserait 2 documents" in str(exc.value)
    assert "1 de moins" in str(exc.value)


def test_option_absente_ne_controle_rien():
    """Sans annonce, la simulation reste libre de découvrir le nombre."""
    plan = construire_plan(_documents(24), [], _cible())

    assert verifier_attendu(plan, None) is None
    assert verifier_attendu(plan, None, ["flux:acte-0"]) is None


def test_liste_vide_de_document_key_ne_passe_pas_inapercue():
    """Le constat du 01/10/2026 : 23 documents annoncés, une liste de clés restée
    vide (`sed` sur un fichier absent), 24 documents dans le plan, dont un
    brouillon hors plan (le Code des assurances CIMA)."""
    documents = _documents(23) + [
        _doc(id="00000000-0000-0000-0000-0000000000ff", document_key="stock:code-cima",
             checksums_sources=frozenset({"sha-cima"}))
    ]
    plan = construire_plan(documents, [], _cible())

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 23, document_keys=())

    message = str(exc.value)
    assert "pousserait 24 documents" in message
    assert "1 de trop" in message
    assert "stock:code-cima" in message


def test_document_hors_liste_est_nomme_en_trop():
    plan = construire_plan(_documents(3), [], _cible())

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 2, document_keys=["flux:acte-0", "flux:acte-1"])

    message = str(exc.value)
    assert "En trop" in message
    assert "flux:acte-2" in message
    assert "Manquant" not in message


def test_cle_ecartee_est_nommee_manquante_avec_son_motif():
    """Une clé demandée que le plan écarte (déjà en cible) fait un compte trop court."""
    documents = _documents(3)
    cible = _cible(ids_documents=frozenset({documents[1].id}))
    plan = construire_plan(documents, [], cible)

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 3, document_keys=[d.document_key for d in documents])

    message = str(exc.value)
    assert "1 de moins" in message
    assert "Manquant" in message
    assert "flux:acte-1" in message
    assert "déjà poussé" in message
    assert "En trop" not in message


def test_cles_conformes_mais_attendu_faux_le_dit():
    documents = _documents(2)
    plan = construire_plan(documents, [], _cible())

    with pytest.raises(PlanInattendu, match="c'est --attendu qui ne correspond pas"):
        verifier_attendu(plan, 5, document_keys=[d.document_key for d in documents])


def test_liste_du_plan_est_tronquee_au_dela_de_la_limite():
    plan = construire_plan(_documents(35), [], _cible())

    with pytest.raises(PlanInattendu) as exc:
        verifier_attendu(plan, 1)

    assert "… et 5 autres" in str(exc.value)
    assert "flux:acte-29" in str(exc.value)
    assert "flux:acte-30" not in str(exc.value)


@pytest.fixture
def push_sans_reseau(monkeypatch):
    """`push-corpus` sans aucun réseau : tout ce qui se connecte est remplacé.

    Les fonctions sont importées dans le corps de la commande, donc remplacer
    l'attribut du module suffit. `executer_push` et `charger_cible_ecriture` sont
    des espions : un refus doit les laisser intacts.
    """
    from unittest.mock import MagicMock

    import src.db.prod_readonly as prod_readonly
    import src.promotion.push_corpus as push_corpus

    documents = _documents(3)
    appels = {"executer_push": [], "cible_ecriture": []}

    def executer_push_espion(*args, **kwargs):
        appels["executer_push"].append(kwargs)
        return {"documents": []}

    def cible_ecriture_espion():
        appels["cible_ecriture"].append(True)
        raise push_corpus.ConfigurationProdManquante("arrêt du test avant toute écriture")

    monkeypatch.setattr(push_corpus, "creer_engine_source", lambda: MagicMock())
    monkeypatch.setattr(prod_readonly, "charger_cible", lambda: MagicMock())
    monkeypatch.setattr(prod_readonly, "creer_engine", lambda cible=None: MagicMock())
    monkeypatch.setattr(prod_readonly, "assert_read_only",
                        lambda engine: prod_readonly.SQLSTATE_LECTURE_SEULE)
    monkeypatch.setattr(push_corpus, "charger_documents_source",
                        lambda engine: (documents, []))
    monkeypatch.setattr(push_corpus, "charger_institutions_par_sigle", lambda engine: {})
    monkeypatch.setattr(push_corpus, "charger_etat_cible", lambda engine: _cible())
    monkeypatch.setattr(push_corpus, "executer_push", executer_push_espion)
    monkeypatch.setattr(push_corpus, "charger_cible_ecriture", cible_ecriture_espion)
    return documents, appels


def _lancer_push(tmp_path, *arguments):
    from click.testing import CliRunner

    from main import cli

    return CliRunner().invoke(
        cli, ["push-corpus", "--rapport", str(tmp_path / "rapport.json"), *arguments]
    )


def test_cli_execute_sans_attendu_est_refuse_avant_toute_connexion(push_sans_reseau, tmp_path):
    """Seconde garde : pas d'écriture en production sans annonce du nombre."""
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--execute")

    assert resultat.exit_code != 0
    assert "--execute exige --attendu" in resultat.output
    assert appels == {"executer_push": [], "cible_ecriture": []}


def test_cli_simulation_trop_grosse_est_refusee_sans_rapport(push_sans_reseau, tmp_path):
    """L'écart saute aux yeux dès la simulation : code non nul, aucun rapport."""
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--attendu", "2")

    assert resultat.exit_code != 0
    assert "1 de trop" in resultat.output
    assert appels["executer_push"] == []
    assert not (tmp_path / "rapport.json").exists()


def test_cli_execute_trop_petite_est_refusee_avant_la_cible_d_ecriture(push_sans_reseau, tmp_path):
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--execute", "--attendu", "4")

    assert resultat.exit_code != 0
    assert "1 de moins" in resultat.output
    assert appels == {"executer_push": [], "cible_ecriture": []}


def test_cli_liste_de_cles_vide_se_voit_dans_le_refus(push_sans_reseau, tmp_path):
    """Reproduction du constat du 01/10/2026 : aucune clé reçue, plan non filtré."""
    documents, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--execute", "--attendu", "2")

    assert resultat.exit_code != 0
    assert "Aucun --document-key" in resultat.output
    assert documents[2].document_key in resultat.output
    assert appels["cible_ecriture"] == []


def test_cli_simulation_conforme_passe_et_le_dit(push_sans_reseau, tmp_path):
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--attendu", "3")

    assert resultat.exit_code == 0, resultat.output
    assert "Attendu : 3, conforme" in resultat.output
    assert len(appels["executer_push"]) == 1


def test_cli_simulation_sans_attendu_reste_libre(push_sans_reseau, tmp_path):
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path)

    assert resultat.exit_code == 0, resultat.output
    assert "Attendu" not in resultat.output
    assert len(appels["executer_push"]) == 1


def test_cli_execute_conforme_atteint_la_cible_d_ecriture(push_sans_reseau, tmp_path):
    """Le contrôle passé, l'exécution continue normalement (ici stoppée par
    l'espion, faute de variables PROD_RW_*)."""
    _, appels = push_sans_reseau

    resultat = _lancer_push(tmp_path, "--execute", "--attendu", "3")

    assert appels["cible_ecriture"] == [True]
    assert resultat.exit_code != 0  # arrêtée par l'espion, pas par --attendu
    assert "de trop" not in resultat.output and "de moins" not in resultat.output


def test_cli_attendu_negatif_est_refuse_par_click(push_sans_reseau, tmp_path):
    resultat = _lancer_push(tmp_path, "--attendu", "-1")

    assert resultat.exit_code != 0
    assert "--attendu" in resultat.output


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


def test_remap_institutions_reecrit_la_fk():
    """dev et prod ont chacun leurs propres UUID pour le même référentiel de 7
    institutions (AN, CC, CS, GOUV, JO, PR, SEN) — constaté en conditions
    réelles le 06/08/2026 : institution_id, jamais remappé, faisait échouer
    tout push d'acte de JO sur legal_documents_institution_id_fkey."""
    expr = _expression_selection(
        "legal_documents", ["institution_id"], {}, {"jo-src": "jo-cible"}
    )

    assert "case institution_id" in expr
    assert "'jo-src'::uuid then 'jo-cible'::uuid" in expr


def test_construire_plan_calcule_le_remap_institutions_par_sigle():
    plan = construire_plan(
        [_doc()], [],
        _cible(institutions_par_sigle={"JO": "id-institution-cible"}),
        institutions_source={"JO": "id-institution-source"},
    )

    assert plan.remap_institutions == {"id-institution-source": "id-institution-cible"}


def test_construire_plan_n_inclut_pas_les_sigles_deja_alignes():
    """Même id des deux côtés : rien à remapper, la CASE ne doit pas exister."""
    plan = construire_plan(
        [_doc()], [],
        _cible(institutions_par_sigle={"JO": "meme-id"}),
        institutions_source={"JO": "meme-id"},
    )

    assert plan.remap_institutions == {}


def test_construire_plan_ignore_un_sigle_absent_de_la_cible():
    plan = construire_plan(
        [_doc()], [],
        _cible(institutions_par_sigle={}),
        institutions_source={"JO": "id-institution-source"},
    )

    assert plan.remap_institutions == {}


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


def test_jo_scinde_les_actes_fratrie_passent_meme_si_un_est_deja_pousse():
    """Un JO scindé produit N documents qui référencent tous le même PDF
    source. Confirmé en conditions réelles le 07/08/2026 : les 12 actes du
    JO 2023-48 partagent un unique SHA-256 — avoir poussé un seul acte
    (la loi n°33-2023) ne doit jamais faire écarter les 11 autres au
    prétexte que « leur » source est déjà en production."""
    checksum_partage = frozenset({"sha-jo-2023-48"})
    fratrie = [
        _doc(id=f"acte-{i}", document_key=f"flux:acte-{i}", checksums_sources=checksum_partage)
        for i in range(3)
    ]
    # Le premier acte est déjà en cible (poussé lors d'une session antérieure),
    # sous un id DIFFÉRENT de celui de la source — d'où la présence de son
    # SHA-256 dans checksums_sources de la cible, mais pas de son id source.
    cible = _cible(checksums_sources=frozenset({"sha-jo-2023-48"}))

    plan = construire_plan(fratrie, [], cible)

    assert {d.id for d in plan.a_pousser} == {"acte-0", "acte-1", "acte-2"}
    assert plan.ecartes == []


def test_vrai_doublon_1_pdf_1_document_reste_ecarte():
    """La règle d'origine garde tout son sens quand le SHA-256 n'est PAS
    partagé par un autre document source : un texte unitaire réingéré sous
    un autre id reste un doublon à écarter."""
    doc_unitaire = _doc(document_key="flux:texte-unitaire", checksums_sources=frozenset({"sha-unique"}))
    cible = _cible(checksums_sources=frozenset({"sha-unique"}))

    plan = construire_plan([doc_unitaire], [], cible)

    assert plan.a_pousser == []
    assert len(plan.ecartes) == 1
    assert "source déjà en production" in plan.ecartes[0][1]
