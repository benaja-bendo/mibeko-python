import os
import click
import uvicorn

@click.group()
def cli():
    """CLI et serveur web pour Mibeko Python"""
    pass


@cli.command()
@click.option('--port', default=8000, help='Port du serveur web')
def serve(port):
    """Lance l'interface web (FastAPI) d'ingestion"""
    click.secho(f"Démarrage de l'interface web sur http://localhost:{port}", fg="green")
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=port, reload=True)


@cli.command("merge-chunks")
@click.option('--folder', required=True, help='Dossier contenant les fichiers chunk_{debut}_a_{fin}.json/.md')
@click.option('--name', default=None, help='Nom de base des fichiers fusionnés (défaut : nom du dossier)')
@click.option('--detect-actes/--no-detect-actes', default=True, help="Repérer les frontières d'Actes uniformes (G1)")
def merge_chunks(folder, name, detect_actes):
    """Fusionne des chunks MinerU (md + json) en un document unique, en ré-offsetant page_idx."""
    from src.extractor.chunk_merger import merge_folder

    if not os.path.isdir(folder):
        click.secho(f"Erreur : dossier introuvable « {folder} »", fg="red")
        return

    try:
        result = merge_folder(folder, output_basename=name, detect_actes=detect_actes)
    except FileNotFoundError as exc:
        click.secho(f"Erreur : {exc}", fg="red")
        return

    click.secho("Fusion terminée.", fg="green")
    click.echo(f"  JSON fusionné : {result['json_path']}")
    click.echo(f"  MD fusionné   : {result['md_path']}")
    click.echo(f"  Pages totales : {result['total_pages']}")

    if result["warnings"]:
        click.secho("  Avertissements de continuité :", fg="yellow")
        for warning in result["warnings"]:
            click.secho(f"    - {warning}", fg="yellow")
    else:
        click.secho("  Continuité des pages : OK (aucun trou ni chevauchement).", fg="green")

    if detect_actes:
        boundaries = result["acte_boundaries"]
        click.secho(f"\n  Actes uniformes détectés (découpage G1) : {len(boundaries)}", fg="cyan")
        for boundary in boundaries:
            click.echo(f"    p.{boundary['page']:>4} — {boundary['title']}")


@cli.command("suggest-boundaries")
@click.option('--json', 'json_path', required=True, help='Chemin du JSON MinerU fusionné')
@click.option('--out', default=None, help='Fichier de bornes à écrire (défaut : <json>.boundaries.json)')
def suggest_boundaries(json_path, out):
    """Propose des bornes d'Actes uniformes (à curer) pour le découpage d'une compilation."""
    import json as _json
    from src.extractor.compilation_splitter import suggest_compilation_boundaries

    if not os.path.isfile(json_path):
        click.secho(f"Erreur : JSON introuvable « {json_path} »", fg="red")
        return

    with open(json_path, encoding="utf-8") as handle:
        data = _json.load(handle)

    boundaries = suggest_compilation_boundaries(data)
    out_path = out or f"{os.path.splitext(json_path)[0]}.boundaries.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        _json.dump(boundaries, handle, ensure_ascii=False, indent=2)

    click.secho(f"{len(boundaries)} borne(s) candidate(s) écrite(s) : {out_path}", fg="green")
    click.secho("→ Relisez/corrigez ce fichier (gardez les vraies pages de début d'acte) avant le découpage.", fg="yellow")
    for boundary in boundaries:
        click.echo(f"    p.{boundary['start_page']:>4} — [{boundary['suggested']}] {boundary['title'][:55]}")


@cli.command("split-compilation")
@click.option('--json', 'json_path', required=True, help='Chemin du JSON MinerU fusionné')
@click.option('--boundaries', 'boundaries_path', required=True, help='Fichier de bornes validées (cf. suggest-boundaries)')
@click.option('--outdir', default=None, help='Dossier de sortie (défaut : <json_dir>/actes)')
def split_compilation(json_path, boundaries_path, outdir):
    """Découpe le JSON fusionné en N sous-JSON (un par Acte) à uploader séparément."""
    import json as _json
    import re as _re
    from src.extractor.compilation_splitter import slice_mineru_json_by_boundaries

    for path in (json_path, boundaries_path):
        if not os.path.isfile(path):
            click.secho(f"Erreur : fichier introuvable « {path} »", fg="red")
            return

    with open(json_path, encoding="utf-8") as handle:
        data = _json.load(handle)
    with open(boundaries_path, encoding="utf-8") as handle:
        boundaries = _json.load(handle)

    slices = slice_mineru_json_by_boundaries(data, boundaries)
    if not slices:
        click.secho("Aucune borne exploitable (start_page manquant ?).", fg="red")
        return

    target_dir = outdir or os.path.join(os.path.dirname(os.path.abspath(json_path)), "actes")
    os.makedirs(target_dir, exist_ok=True)

    click.secho(f"{len(slices)} acte(s) → {target_dir}", fg="green")
    for index, (boundary, sub_json) in enumerate(slices, start=1):
        label = boundary.get("suggested") or boundary.get("title") or f"acte-{index}"
        slug = _re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or f"acte-{index}"
        out_name = f"acte_{index:02d}_{slug}.json"
        out_path = os.path.join(target_dir, out_name)
        with open(out_path, "w", encoding="utf-8") as handle:
            _json.dump(sub_json, handle, ensure_ascii=False)
        meta = sub_json["_mibeko_split"]
        click.echo(f"    {out_name}  (p.{meta['page_start']}–{meta['page_end'] or 'fin'}, {meta['page_count']} pages)")

    click.secho("→ Uploadez chaque fichier via /editor/ingestion (type auto-détecté AU).", fg="cyan")


@cli.command("suggest-boundaries-md")
@click.option('--md', 'md_path', required=True, help='Chemin du markdown MinerU (recueil)')
@click.option('--out', default=None, help='Fichier de bornes à écrire (défaut : <md>.boundaries.json)')
def suggest_boundaries_md(md_path, out):
    """Propose des bornes d'actes encapsulés (LOI/ORDONNANCE/DÉCRET/CODE…) depuis un markdown."""
    import json as _json
    from src.extractor.compilation_splitter import suggest_markdown_boundaries

    if not os.path.isfile(md_path):
        click.secho(f"Erreur : markdown introuvable « {md_path} »", fg="red")
        return

    with open(md_path, encoding="utf-8") as handle:
        markdown_text = handle.read()

    boundaries = suggest_markdown_boundaries(markdown_text)
    out_path = out or f"{os.path.splitext(md_path)[0]}.boundaries.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        _json.dump(boundaries, handle, ensure_ascii=False, indent=2)

    click.secho(f"{len(boundaries)} borne(s) candidate(s) écrite(s) : {out_path}", fg="green")
    click.secho("→ Relisez/corrigez ce fichier (gardez les vraies lignes de début d'acte) avant le découpage.", fg="yellow")
    for boundary in boundaries:
        click.echo(f"    L{boundary['start_line']:>5} — [{boundary['type_code']}] {boundary['title'][:55]}")


@cli.command("split-compilation-md")
@click.option('--md', 'md_path', required=True, help='Chemin du markdown (recueil)')
@click.option('--boundaries', 'boundaries_path', required=True, help='Fichier de bornes validées (cf. suggest-boundaries-md)')
@click.option('--outdir', default=None, help='Dossier de sortie (défaut : <md_dir>/actes)')
def split_compilation_md(md_path, boundaries_path, outdir):
    """Découpe le markdown en N sous-markdowns (un par acte) à uploader séparément."""
    import json as _json
    import re as _re
    from src.extractor.compilation_splitter import slice_markdown_by_boundaries

    for path in (md_path, boundaries_path):
        if not os.path.isfile(path):
            click.secho(f"Erreur : fichier introuvable « {path} »", fg="red")
            return

    with open(md_path, encoding="utf-8") as handle:
        markdown_text = handle.read()
    with open(boundaries_path, encoding="utf-8") as handle:
        boundaries = _json.load(handle)

    slices = slice_markdown_by_boundaries(markdown_text, boundaries)
    if not slices:
        click.secho("Aucune borne exploitable (start_line manquant ?).", fg="red")
        return

    target_dir = outdir or os.path.join(os.path.dirname(os.path.abspath(md_path)), "actes")
    os.makedirs(target_dir, exist_ok=True)

    click.secho(f"{len(slices)} acte(s) → {target_dir}", fg="green")
    for index, (boundary, sub_text) in enumerate(slices, start=1):
        meta = boundary["_mibeko_split"]
        label = boundary.get("suggested") or meta.get("title") or f"acte-{index}"
        slug = _re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or f"acte-{index}"
        out_name = f"acte_{index:02d}_{slug}.md"
        out_path = os.path.join(target_dir, out_name)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(sub_text)
        click.echo(f"    {out_name}  (L{meta['line_start']}-{meta['line_end']}, {meta['line_count']} lignes, type {meta['type_code']})")

    click.secho("→ Uploadez chaque fichier via /editor/ingestion (type pré-rempli par acte).", fg="cyan")


@cli.command("backfill-manifest")
@click.option('--data-dir', 'data_dir_opt', default=None, help='Dossier data/ (défaut : ../data ou MIBEKO_DATA_DIR)')
def backfill_manifest(data_dir_opt):
    """Rétro-remplit les manifestes de provenance depuis data/sources/ existant."""
    from pathlib import Path
    from src.acquisition.backfill import backfill
    from src.acquisition.config import data_dir

    target = Path(data_dir_opt).resolve() if data_dir_opt else data_dir()
    if not (target / "sources").is_dir():
        click.secho(f"Erreur : {target}/sources introuvable", fg="red")
        raise SystemExit(1)

    click.secho(f"Rétro-remplissage depuis {target}/sources …", fg="cyan")
    summary = backfill(target)
    click.secho(f"Ajoutés : {summary['ajoutes']} · déjà connus : {summary['existants']} · doublons SHA : {summary['doublons_sha']}", fg="green")
    for key, count in summary["manifestes"].items():
        click.echo(f"    manifests/{key}.jsonl : {count} entrée(s)")
    if summary["hors_grammaire_jo"]:
        click.secho(f"  JO hors grammaire (URL non reconstruite) : {len(summary['hors_grammaire_jo'])}", fg="yellow")
        for name in summary["hors_grammaire_jo"]:
            click.echo(f"    - {name}")


@cli.command("acquire")
@click.option('--carnet', 'carnet_opt', default=None, help='Carnet YAML (défaut : data/corpus/corpus-v1.yaml)')
@click.option('--source', 'source_key', default=None, help="Limiter à une série du carnet (ex. jo-recents) ou 'textes'")
@click.option('--dry-run', is_flag=True, help='Découverte seule : liste ce qui serait téléchargé')
@click.option('--limit', default=None, type=int, help='Plafond de téléchargements pour cette exécution')
def acquire(carnet_opt, source_key, dry_run, limit):
    """Acquiert les textes du carnet (sgg.cg) avec provenance. Idempotent."""
    import json as _json
    from pathlib import Path
    from src.acquisition.acquire import run_acquire
    from src.acquisition.config import corpus_file, data_dir

    carnet_path = Path(carnet_opt).resolve() if carnet_opt else corpus_file()
    if not carnet_path.is_file():
        click.secho(f"Erreur : carnet introuvable « {carnet_path} »", fg="red")
        raise SystemExit(1)

    click.secho(f"Acquisition pilotée par {carnet_path}{' (dry-run)' if dry_run else ''} …", fg="cyan")
    report = run_acquire(carnet_path, data_dir(), source_key=source_key, dry_run=dry_run, limit=limit)
    click.echo(_json.dumps(report, ensure_ascii=False, indent=2))


@cli.command("ohada-recon")
@click.option('--out', default=None, help='Rapport markdown (défaut : data/manifests/ohada-recon.md)')
def ohada_recon(out):
    """Reconnaissance ohada.org : liste les liens candidats, sans rien télécharger."""
    from pathlib import Path
    from src.acquisition.config import data_dir
    from src.acquisition.ohada import recon
    from src.acquisition.politeness import PoliteClient

    out_path = Path(out).resolve() if out else data_dir() / "manifests" / "ohada-recon.md"
    with PoliteClient() as client:
        report = recon(client, out_path)
    click.secho(f"{len(report['pdfs'])} PDF et {len(report['pages'])} pages candidates → {out_path}", fg="green")
    click.secho("→ Valider ce rapport avant d'inscrire les URLs dans le carnet.", fg="yellow")


@cli.command("process-batch")
@click.option('--source', 'source_key', default=None, help="Limiter à un manifeste (ex. sgg-jo)")
@click.option('--limit', default=None, type=int, help='Plafond de documents traités pour cette exécution')
@click.option('--dry-run', is_flag=True, help='Triage seul : prévisualise natif vs MinerU, aucune écriture')
@click.option('--force', is_flag=True, help='Retraiter même si déjà à jour (SHA source inchangé)')
@click.option(
    '--include-hors-perimetre', is_flag=True,
    help="Inclure aussi les traités internationaux/CEMAC et lots privés (exclus du périmètre v1 par défaut)",
)
def process_batch(source_key, limit, dry_run, force, include_hors_perimetre):
    """Triage (natif → MinerU) du carnet, piloté par le manifeste. Idempotent."""
    import json as _json
    from src.acquisition.config import data_dir
    from src.parsing.batch import dry_run_report, run_batch

    target = data_dir()

    if dry_run:
        report = dry_run_report(
            target, source_key=source_key, limit=limit, include_hors_perimetre=include_hors_perimetre
        )
        click.echo(_json.dumps(report, ensure_ascii=False, indent=2))
        native = sum(1 for r in report if r.get("methode_prevue") == "native")
        mineru = sum(1 for r in report if r.get("methode_prevue") == "mineru")
        erreurs = sum(1 for r in report if "erreur" in r)
        click.secho(f"{len(report)} document(s) : {native} natif, {mineru} MinerU, {erreurs} erreur(s)", fg="cyan")
        return

    click.secho("Traitement du carnet (triage natif → MinerU si besoin) …", fg="cyan")
    summary = run_batch(
        target, source_key=source_key, limit=limit, force=force, include_hors_perimetre=include_hors_perimetre
    )
    click.secho(
        f"Traités : {summary['traites']} · sautés (déjà à jour) : {summary['sautes']} · "
        f"hors périmètre v1 (ignorés) : {summary['hors_perimetre']} · erreurs : {len(summary['erreurs'])}",
        fg="green",
    )
    for methode, count in summary["par_methode"].items():
        click.echo(f"    {methode} : {count}")
    for err in summary["erreurs"]:
        click.secho(f"    ✗ {err['id']} : {err['erreur']}", fg="red")


@cli.command("structure-batch")
@click.option('--source', 'source_key', default=None, help="Limiter à un manifeste (ex. sgg-jo)")
@click.option('--limit', default=None, type=int, help='Plafond de documents traités pour cette exécution')
@click.option('--dry-run', is_flag=True, help='Parsing + LLM + validation seuls, aucune écriture')
@click.option('--force', is_flag=True, help='Retraiter même si déjà à jour (sans effet : idempotence par document_key)')
@click.option(
    '--include-hors-perimetre', is_flag=True,
    help="Inclure aussi les traités internationaux/CEMAC et lots privés (exclus du périmètre v1 par défaut)",
)
def structure_batch(source_key, limit, dry_run, force, include_hors_perimetre):
    """Structuration (parseur + Mistral) du carnet, piloté par le manifeste. Idempotent."""
    import json as _json
    from src.acquisition.config import data_dir
    from src.db.database import SessionLocal
    from src.structuration.batch import dry_run_report, run_batch

    target = data_dir()
    db = SessionLocal()
    try:
        if dry_run:
            report = dry_run_report(
                db, target, source_key=source_key, limit=limit, include_hors_perimetre=include_hors_perimetre
            )
            click.echo(_json.dumps(report, ensure_ascii=False, indent=2))
            valides = sum(1 for r in report if r.get("statut_prevu") == "structure")
            erreurs = sum(1 for r in report if r.get("statut_prevu") == "erreur")
            click.secho(f"{len(report)} document(s) : {valides} validé(s), {erreurs} erreur(s)", fg="cyan")
            return

        click.secho("Structuration du carnet (parseur + Mistral) …", fg="cyan")
        summary = run_batch(
            db, target, source_key=source_key, limit=limit, force=force, include_hors_perimetre=include_hors_perimetre
        )
        click.secho(
            f"Traités : {summary['traites']} · déjà existants : {summary['deja_existants']} · "
            f"hors périmètre v1 (ignorés) : {summary['hors_perimetre']} · erreurs : {len(summary['erreurs'])}",
            fg="green",
        )
        for err in summary["erreurs"]:
            click.secho(f"    ✗ {err['id']} : {err['erreur']}", fg="red")
    finally:
        db.close()


if __name__ == "__main__":
    cli()
