import os
from datetime import datetime

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


@cli.command("prod-preflight")
def prod_preflight():
    """Contrôle une session de diagnostic sur la PROD : prouve la lecture seule puis l'état des lieux."""
    from src.db.prod_readonly import (
        CibleProdAmbigue,
        ConfigurationProdManquante,
        LectureSeuleNonProuvee,
        SQLSTATE_LECTURE_SEULE,
        assert_read_only,
        charger_cible,
        creer_engine,
        etat_des_lieux,
    )

    click.secho("\n  ███  PRODUCTION  ███\n", fg="red", bold=True)

    try:
        cible = charger_cible()
    except ConfigurationProdManquante as exc:
        click.secho(f"Configuration incomplète : {exc}", fg="red")
        raise SystemExit(1)
    except CibleProdAmbigue as exc:
        click.secho(f"Cible ambiguë : {exc}", fg="red")
        raise SystemExit(1)

    # Le mot de passe n'est jamais affiché, et le DSN complet jamais journalisé.
    click.echo(f"  Cible : {cible.resume()}")
    click.echo("  Mode attendu : lecture seule\n")

    engine = creer_engine(cible)

    try:
        sqlstate = assert_read_only(engine)
    except LectureSeuleNonProuvee as exc:
        click.secho(f"Lecture seule NON prouvée : {exc}", fg="red")
        raise SystemExit(1)
    except Exception as exc:
        click.secho(f"Connexion impossible : {exc}", fg="red")
        click.secho(
            f"Le tunnel SSH est-il ouvert ? Vérifier : lsof -nP -iTCP:{cible.port} -sTCP:LISTEN",
            fg="yellow",
        )
        raise SystemExit(1)

    if sqlstate == SQLSTATE_LECTURE_SEULE:
        click.secho("Lecture seule prouvée (SQLSTATE 25006).", fg="green")
    else:
        click.secho(f"Écriture refusée par privilège insuffisant (SQLSTATE {sqlstate}).", fg="green")
        click.secho(
            "La session n'est pas marquée en lecture seule, mais le rôle ne peut pas écrire. "
            "Recommandé : ALTER ROLE ... SET default_transaction_read_only = on.",
            fg="yellow",
        )

    etat = etat_des_lieux(engine)

    click.secho(f"\nPostgreSQL : {etat['version_postgres']}", fg="cyan")
    for nom, presente in etat["extensions"].items():
        click.secho(f"    {nom:<12} {'oui' if presente else 'NON'}", fg="green" if presente else "red")

    click.secho("\nDocuments par statut de curation (vivants / soft-deleted) :", fg="cyan")
    for statut, vivants, supprimes in etat["documents_par_statut"]:
        click.echo(f"    {statut or '(nul)':<12} {vivants:>6} / {supprimes}")

    click.secho("\nAnomalies non résolues par gravité :", fg="cyan")
    if etat["anomalies_non_resolues"]:
        for severity, total in etat["anomalies_non_resolues"]:
            click.echo(f"    {severity or '(nul)':<12} {total}")
    else:
        click.echo("    (aucune)")

    click.secho("\nCompteurs :", fg="cyan")
    click.echo(f"    Articles vivants          : {etat['articles_vivants']}")
    click.echo(f"    Articles soft-deleted     : {etat['articles_supprimes']}")
    click.echo(f"    Versions d'articles       : {etat['versions_articles']}")
    click.echo(f"    Versions sans embedding   : {etat['versions_sans_embedding']}")
    click.echo(f"    Journaux officiels        : {etat['journaux_officiels']}")
    click.echo(f"    Fichiers média            : {etat['fichiers_media']}")

    click.secho("\nRègles de la session :", fg="yellow")
    click.echo("    · Lecture seule par défaut : le diagnostic ne modifie jamais la production.")
    click.echo("    · Toute écriture exige une autorisation humaine explicite, opération par opération.")
    click.echo("    · Une autorisation ne vaut que pour l'opération nommée, jamais pour la suivante.")
    click.echo("    · Toute écriture est précédée d'un dump frais et livrée sous forme rejouable.")
    click.echo("    · La publication passe par l'API Laravel, jamais par un UPDATE de curation_status.")


@cli.command("push-corpus")
@click.option("--execute", "executer", is_flag=True,
              help="Écrit réellement dans la cible (défaut : dry-run, aucune écriture).")
@click.option("--limit", "limite", type=int, default=None,
              help="Ne pousser que les N premiers documents du plan (lot pilote).")
@click.option("--rapport", "rapport_chemin", default=None,
              help="Chemin du rapport JSON (défaut : data/pipeline/meta/push-<horodatage>.json).")
def push_corpus(executer, limite, rapport_chemin):
    """Pousse le corpus validé du dev vers la PROD, de façon additive (staging only).

    Dry-run par défaut : le plan est calculé via le profil de lecture seule
    (PROD_RO_*), aucune écriture. Avec --execute, les identifiants d'écriture
    PROD_RW_DB_* et PROD_RW_MINIO_* doivent être exportés dans le shell — jamais
    dans un fichier — et une confirmation interactive est exigée.
    """
    from src.db.prod_readonly import (
        SQLSTATE_LECTURE_SEULE,
        assert_read_only,
        charger_cible,
        creer_engine,
    )
    from src.promotion.push_corpus import (
        CibleProdAmbigue,
        ConfigurationProdManquante,
        charger_cible_ecriture,
        charger_documents_source,
        charger_etat_cible,
        construire_plan,
        creer_client_minio_ecriture,
        creer_client_minio_source,
        creer_engine_source,
        executer_push,
    )

    click.secho("\n  ███  PUSH CORPUS → PRODUCTION  ███\n", fg="red", bold=True)

    try:
        engine_source = creer_engine_source()

        # Le plan se calcule TOUJOURS sur le profil de lecture seule : même en
        # mode --execute, on prouve d'abord que ce profil ne peut pas écrire —
        # préflight intégré, pas optionnel.
        cible_ro = charger_cible()
        engine_ro = creer_engine(cible_ro)
        sqlstate = assert_read_only(engine_ro)
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        click.secho(f"Refus : {exc}", fg="red")
        raise SystemExit(1)
    if sqlstate == SQLSTATE_LECTURE_SEULE:
        click.secho("Préflight : lecture seule prouvée (SQLSTATE 25006).", fg="green")

    documents, journaux = charger_documents_source(engine_source)
    plan = construire_plan(documents, journaux, charger_etat_cible(engine_ro))

    click.secho(f"\n  Source : {len(documents)} documents vivants", fg="cyan")
    click.secho(f"  À pousser : {len(plan.a_pousser)}"
                + (f" (limité à {limite})" if limite else ""), fg="cyan")
    click.secho(f"  Écartés : {len(plan.ecartes)}", fg="cyan")
    for doc, motif in plan.ecartes:
        click.echo(f"    − {doc.libelle()} : {motif}")
    if plan.journaux_a_creer:
        click.secho(f"  Journaux officiels à créer : {len(plan.journaux_a_creer)}", fg="cyan")
    if plan.remap_journaux:
        click.secho(f"  Journaux rattachés à une fiche existante : {len(plan.remap_journaux)}", fg="cyan")

    if rapport_chemin is None:
        meta_dir = os.path.join(os.getenv("MIBEKO_DATA_DIR", "data/"), "pipeline", "meta")
        os.makedirs(meta_dir, exist_ok=True)
        horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
        rapport_chemin = os.path.join(meta_dir, f"push-{horodatage}.json")

    if not executer:
        rapport = executer_push(
            engine_source, engine_ro, plan, dry_run=True, limite=limite,
            rapport_chemin=rapport_chemin,
        )
        total_lignes = sum(sum(e["tables"].values()) for e in rapport["documents"])
        total_objets = sum(len(e["objets"]) for e in rapport["documents"])
        click.secho(
            f"\nDRY-RUN — aucune écriture. {len(rapport['documents'])} documents, "
            f"{total_lignes} lignes et {total_objets} objets MinIO seraient poussés.",
            fg="yellow",
        )
        click.echo(f"Rapport : {rapport_chemin}")
        click.secho("\nPour exécuter : exporter PROD_RW_DB_* et PROD_RW_MINIO_* dans le "
                    "shell (jamais dans un fichier), prendre un dump frais (runbook §4), "
                    "puis relancer avec --execute.", fg="yellow")
        return

    # ------------------------------ Mode écriture ------------------------------
    try:
        engine_rw = charger_cible_ecriture()
        minio_source = creer_client_minio_source()
        minio_cible = creer_client_minio_ecriture()
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        click.secho(f"Refus : {exc}", fg="red")
        raise SystemExit(1)

    # La cible RW doit être LA MÊME base que celle inspectée en RO : on compare
    # un invariant lisible plutôt que de faire confiance aux variables.
    from sqlalchemy import text
    with engine_rw.connect() as cnx_rw, engine_ro.connect() as cnx_ro:
        compte = "select count(*) from legal_documents"
        if cnx_rw.execute(text(compte)).scalar() != cnx_ro.execute(text(compte)).scalar():
            click.secho("Refus : la cible RW ne répond pas comme la cible RO — "
                        "les deux profils ne visent pas la même base.", fg="red")
            raise SystemExit(1)

    click.secho(f"\n  {len(plan.a_pousser[:limite] if limite else plan.a_pousser)} "
                "documents vont être ÉCRITS en production (staging, draft).", fg="red")
    saisie = click.prompt("Taper PRODUCTION pour confirmer", default="", show_default=False)
    if saisie != "PRODUCTION":
        click.secho("Annulé.", fg="yellow")
        raise SystemExit(1)

    rapport = executer_push(
        engine_source, engine_rw, plan,
        client_minio_source=minio_source, client_minio_cible=minio_cible,
        dry_run=False, limite=limite, rapport_chemin=rapport_chemin,
    )
    total_lignes = sum(sum(e["tables"].values()) for e in rapport["documents"])
    click.secho(f"\nPoussé : {len(rapport['documents'])} documents, {total_lignes} lignes.",
                fg="green")
    click.echo(f"Rapport : {rapport_chemin}")
    click.secho("Vérifier ensuite avec prod-preflight, puis publier via l'éditeur.", fg="yellow")


if __name__ == "__main__":
    cli()
