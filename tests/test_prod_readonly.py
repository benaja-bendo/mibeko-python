"""Garde-fous du profil de diagnostic sur la base de PRODUCTION.

Aucun test ici n'ouvre de connexion réseau : ce qui est vérifiable hors production,
c'est que le module refuse de partir sur une configuration incomplète ou ambiguë,
qu'il ne construit rien à l'import, et qu'il ne conclut jamais « la session est
sûre » sur un échec d'écriture dont le motif ne prouve rien.

La preuve réelle de lecture seule (SQLSTATE 25006 / 42501) ne peut être obtenue
que contre un vrai serveur : c'est le rôle du préflight à l'exécution.
"""

import os

import pytest

from src.db import prod_readonly
from src.db.prod_readonly import (
    SQLSTATE_LECTURE_SEULE,
    SQLSTATE_PRIVILEGE_INSUFFISANT,
    CibleProd,
    CibleProdAmbigue,
    ConfigurationProdManquante,
    LectureSeuleNonProuvee,
    assert_read_only,
    charger_cible,
)

_VARIABLES_PROD = (
    "PROD_RO_DB_HOST",
    "PROD_RO_DB_PORT",
    "PROD_RO_DB_DATABASE",
    "PROD_RO_DB_USERNAME",
    "PROD_RO_DB_PASSWORD",
)


@pytest.fixture
def env_prod(monkeypatch):
    """Profil de diagnostic complet et non ambigu (dev sur 5433, prod sur 5434)."""
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_DATABASE", "mibeko-db")

    # Même hôte et même nom de base que le dev : c'est la réalité du tunnel SSH
    # (la base de production s'appelle aussi mibeko-db). Seul le port discrimine.
    monkeypatch.setenv("PROD_RO_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("PROD_RO_DB_PORT", "5434")
    monkeypatch.setenv("PROD_RO_DB_DATABASE", "mibeko-db")
    monkeypatch.setenv("PROD_RO_DB_USERNAME", "mibeko_ro")
    monkeypatch.setenv("PROD_RO_DB_PASSWORD", "secret-de-test")
    return monkeypatch


@pytest.mark.parametrize("variable", _VARIABLES_PROD[2:])
def test_variable_obligatoire_absente_refuse(env_prod, variable):
    """Une cible incomplète doit être refusée avec le nom de la variable fautive."""
    env_prod.delenv(variable, raising=False)

    with pytest.raises(ConfigurationProdManquante) as exc:
        charger_cible()

    assert variable in str(exc.value)


def test_port_identique_au_developpement_refuse(env_prod):
    """Le scénario d'accident : un tunnel prod ouvert sur le port du dev."""
    env_prod.setenv("PROD_RO_DB_PORT", "5433")

    with pytest.raises(CibleProdAmbigue) as exc:
        charger_cible()

    assert "développement" in str(exc.value)


def test_meme_hote_et_meme_nom_de_base_sur_port_distinct_accepte(env_prod):
    """La configuration du runbook : tunnel SSH sur 5434, base nommée comme en dev.

    À travers un tunnel, dev et prod partagent l'hôte (127.0.0.1) et le nom de
    base (mibeko-db) : seul le port les distingue. Refuser cette forme rendrait
    le diagnostic impossible — c'est le port, et lui seul, qui discrimine.
    """
    cible = charger_cible()

    assert cible.port == "5434"
    assert cible.database == "mibeko-db"


def test_cible_valide_est_chargee(env_prod):
    cible = charger_cible()

    assert cible.host == "127.0.0.1"
    assert cible.port == "5434"
    assert cible.database == "mibeko-db"
    assert cible.username == "mibeko_ro"


def test_dsn_construit_correctement(env_prod):
    cible = charger_cible()

    assert cible.dsn() == "postgresql://mibeko_ro:secret-de-test@127.0.0.1:5434/mibeko-db"


def test_le_mot_de_passe_ne_fuit_ni_dans_le_resume_ni_dans_le_repr(env_prod):
    """Le résumé est fait pour être affiché : il ne doit jamais porter le secret."""
    cible = charger_cible()

    assert "secret-de-test" not in cible.resume()
    assert "secret-de-test" not in repr(cible)
    assert "mibeko_ro@127.0.0.1:5434/mibeko-db" == cible.resume()


def test_l_import_ne_cree_ni_engine_ni_client():
    """Importer le module à froid ne doit toucher aucun service.

    Le module de diagnostic doit rester importable sans effet de bord : src/db/database.py
    crée son engine dès l'import, et le singleton MinIO du pipeline appelle make_bucket
    dès l'import — avec un tunnel ouvert, cela toucherait la production.

    La vérification se fait dans un SOUS-PROCESSUS, pour deux raisons : c'est le seul
    moyen d'observer un interpréteur réellement froid, et un `importlib.reload` dans le
    processus de test recréerait les classes d'exception du module, cassant les
    `pytest.raises` des tests suivants (l'ancienne classe ne correspondrait plus).
    """
    import subprocess
    import sys
    from pathlib import Path

    verification = (
        "import sys\n"
        "import src.db.prod_readonly as m\n"
        "interdits = [n for n in ('src.db.database', 'src.db.models',"
        " 'src.services.minio_service') if n in sys.modules]\n"
        "assert not interdits, 'imports à effet de bord : %s' % interdits\n"
        "assert not hasattr(m, 'engine'), \"l'engine doit être construit à la demande\"\n"
        "assert not hasattr(m, 'minio_service'), 'aucun client MinIO à l\\'import'\n"
        "print('ok')\n"
    )

    racine = Path(__file__).resolve().parent.parent
    resultat = subprocess.run(
        [sys.executable, "-c", verification],
        cwd=racine,
        capture_output=True,
        text=True,
    )

    assert resultat.returncode == 0, (
        "l'import à froid du profil de diagnostic a des effets de bord :\n"
        f"{resultat.stdout}{resultat.stderr}"
    )


def test_le_port_de_diagnostic_par_defaut_nest_pas_celui_du_dev():
    assert prod_readonly.PORT_PROD_PAR_DEFAUT == "5434"
    assert prod_readonly.PORT_PROD_PAR_DEFAUT != os.getenv("DB_PORT", "5433")
    assert prod_readonly.ENDPOINT_MINIO_PAR_DEFAUT.endswith(":9100")


class _FauxOrig:
    """Imite psycopg2 : porte un SQLSTATE dans pgcode."""

    def __init__(self, pgcode):
        self.pgcode = pgcode


class _FausseErreurDBAPI(Exception):
    def __init__(self, pgcode):
        super().__init__(f"erreur simulée (SQLSTATE {pgcode})")
        self.orig = _FauxOrig(pgcode)


class _FausseConnexion:
    """Connexion minimale traçant l'annulation et la fermeture."""

    def __init__(self, erreur=None):
        self._erreur = erreur
        self.rollback_appele = False
        self.close_appele = False

    def begin(self):
        connexion = self

        class _Transaction:
            def rollback(self):
                connexion.rollback_appele = True

        return _Transaction()

    def execute(self, _requete):
        if self._erreur is not None:
            raise self._erreur
        return None

    def close(self):
        self.close_appele = True


class _FauxEngine:
    def __init__(self, connexion):
        self._connexion = connexion

    def connect(self):
        return self._connexion


@pytest.fixture
def dbapi_error(monkeypatch):
    """Fait passer notre fausse exception pour un DBAPIError SQLAlchemy."""
    import sqlalchemy.exc

    monkeypatch.setattr(sqlalchemy.exc, "DBAPIError", _FausseErreurDBAPI)
    return _FausseErreurDBAPI


def test_lecture_seule_prouvee_par_sqlstate_25006(dbapi_error):
    connexion = _FausseConnexion(erreur=dbapi_error(SQLSTATE_LECTURE_SEULE))

    assert assert_read_only(_FauxEngine(connexion)) == SQLSTATE_LECTURE_SEULE
    assert connexion.rollback_appele, "la sonde doit toujours être annulée"
    assert connexion.close_appele


def test_privilege_insuffisant_accepte_comme_preuve(dbapi_error):
    connexion = _FausseConnexion(erreur=dbapi_error(SQLSTATE_PRIVILEGE_INSUFFISANT))

    assert assert_read_only(_FauxEngine(connexion)) == SQLSTATE_PRIVILEGE_INSUFFISANT
    assert connexion.rollback_appele


def test_echec_pour_un_autre_motif_ne_prouve_rien(dbapi_error):
    """Le piège du faux négatif : une table absente (42P01) ne prouve pas la sûreté."""
    connexion = _FausseConnexion(erreur=dbapi_error("42P01"))

    with pytest.raises(LectureSeuleNonProuvee) as exc:
        assert_read_only(_FauxEngine(connexion))

    assert "ne prouve rien" in str(exc.value)
    assert connexion.rollback_appele, "la sonde doit être annulée même en cas d'échec"


def test_ecriture_acceptee_leve_une_alerte():
    connexion = _FausseConnexion(erreur=None)

    with pytest.raises(LectureSeuleNonProuvee) as exc:
        assert_read_only(_FauxEngine(connexion))

    assert "ACCEPTÉE" in str(exc.value)
    assert connexion.rollback_appele, "une sonde acceptée doit impérativement être annulée"


def test_client_minio_refuse_le_port_du_developpement(monkeypatch):
    monkeypatch.setenv("PROD_RO_MINIO_ACCESS_KEY", "cle-de-test")
    monkeypatch.setenv("PROD_RO_MINIO_SECRET_KEY", "secret-de-test")
    monkeypatch.setenv("PROD_RO_MINIO_ENDPOINT", "127.0.0.1:9000")

    with pytest.raises(CibleProdAmbigue):
        prod_readonly.creer_client_minio_diagnostic()


def test_client_minio_exige_ses_identifiants(monkeypatch):
    monkeypatch.delenv("PROD_RO_MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PROD_RO_MINIO_SECRET_KEY", raising=False)

    with pytest.raises(ConfigurationProdManquante) as exc:
        prod_readonly.creer_client_minio_diagnostic()

    assert "PROD_RO_MINIO_ACCESS_KEY" in str(exc.value)


def test_cible_prod_est_immuable():
    """Une cible chargée ne doit pas pouvoir être déviée après coup."""
    import dataclasses

    cible = CibleProd(
        host="127.0.0.1",
        port="5434",
        database="mibeko-db",
        username="mibeko_ro",
        password="secret-de-test",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        cible.host = "autre-hote"  # type: ignore[misc]
