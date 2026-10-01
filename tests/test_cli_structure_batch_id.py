"""`structure-batch --id` (mibeko-python#39) : une faute de frappe dans un
identifiant doit échouer bruyamment, pas passer pour « rien à structurer »."""

from click.testing import CliRunner

from main import cli


def test_structure_batch_refuse_un_id_absent_des_manifestes(tmp_path, monkeypatch):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "sgg-jo.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("MIBEKO_DATA_DIR", str(tmp_path))

    resultat = CliRunner().invoke(cli, ["structure-batch", "--id", "sgg-jo/faute-de-frappe", "--dry-run"])

    assert resultat.exit_code == 1
    assert "Refus" in resultat.output
    assert "sgg-jo/faute-de-frappe" in resultat.output


def test_l_aide_de_structure_batch_decrit_id():
    resultat = CliRunner().invoke(cli, ["structure-batch", "--help"])

    assert resultat.exit_code == 0
    assert "--id" in resultat.output
