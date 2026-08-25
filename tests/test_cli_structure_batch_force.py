"""Vérifie que `structure-batch` n'accepte plus de retraitement forcé.

L'option `--force` a existé jusqu'au 25/08/2026 : acceptée en ligne de commande,
transmise jusqu'à `run_batch`, et purement ignorée — elle donnait l'illusion de
forcer un retraitement que l'idempotence par `document_key` rendait impossible
(mibeko-python#4).

Elle est supprimée plutôt que rendue fonctionnelle : la contourner reviendrait à
créer un second document pour le même texte, précisément ce que cette clé existe
pour empêcher. Ce test verrouille les deux moitiés de la décision — l'option a
bien disparu de `structure-batch`, et elle reste en place sur `process-batch` où
elle fait réellement quelque chose.

Exécutable sans base :  python3 tests/test_cli_structure_batch_force.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from click.testing import CliRunner  # noqa: E402

from main import cli  # noqa: E402


def test_structure_batch_refuse_l_option_force():
    # Échouer bruyamment vaut mieux que ne rien faire en silence : un opérateur
    # qui tape `--force` doit l'apprendre tout de suite, pas découvrir après
    # coup que rien n'a été retraité.
    resultat = CliRunner().invoke(cli, ["structure-batch", "--force", "--dry-run"])

    assert resultat.exit_code != 0
    assert "No such option: --force" in resultat.output


def test_l_aide_de_structure_batch_ne_mentionne_plus_force():
    resultat = CliRunner().invoke(cli, ["structure-batch", "--help"])

    assert resultat.exit_code == 0
    assert "--force" not in resultat.output


def test_process_batch_conserve_son_force_qui_lui_agit():
    # `process-batch` saute une entrée dont le SHA source est inchangé, sauf
    # `force=True` : là, l'option a un effet, elle reste.
    resultat = CliRunner().invoke(cli, ["process-batch", "--help"])

    assert resultat.exit_code == 0
    assert "--force" in resultat.output


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as erreur:
            failures += 1
            print(f"  FAIL {test.__name__}: {erreur}")
    sys.exit(1 if failures else 0)
