import os
import shutil
import click
import uuid
import uvicorn
import hashlib
from datetime import datetime
from sqlalchemy.orm import Session
from psycopg2.extras import DateRange

from src.db.database import SessionLocal, init_db
from src.db.models import Institution, LegalDocument, MediaFile, ExtractionRun, StructureNode, Article, ArticleVersion
from src.extractor.parser import LegalDocumentParser

@click.group()
def cli():
    """CLI et serveur web pour Mibeko Python"""
    pass


def compute_sha256(file_path: str) -> str:
    """Calcule l'empreinte SHA-256 d'un fichier local."""

    digest = hashlib.sha256()
    with open(file_path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(8192), b""):
            digest.update(chunk)

    return digest.hexdigest()

@cli.command()
@click.option('--port', default=8000, help='Port du serveur web')
def serve(port):
    """Lance l'interface web (FastAPI) d'ingestion"""
    click.secho(f"Démarrage de l'interface web sur http://localhost:{port}", fg="green")
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=port, reload=True)

@cli.command()
@click.option('--path', default='data/congo-code-1975-travail.pdf', help='Chemin absolu ou relatif vers le PDF du Code du Travail')
@click.option('--title', default='Code du Travail', help='Titre du document juridique')
@click.option('--publication-date', help='Date de publication (YYYY-MM-DD)')
@click.option('--institution-sigle', default='METP', help='Sigle de l’institution')
@click.option('--document-key', default='code-travail-1975', help='Clé stable pour éviter les doublons')
@click.option('--sync', is_flag=True, default=True, help='Exécuter l’extraction immédiatement')
def simulate_code_du_travail(path, title, publication_date, institution_sigle, document_key, sync):
    """
    Simule la création d’un document juridique (upload PDF + job d’extraction)
    et parse le document avec spaCy et PyMuPDF pour l'insérer dans la BDD PostgreSQL de Laravel.
    """
    click.echo(f"Démarrage de l'import et de l'extraction pour: {title}")

    if not os.path.isfile(path):
        click.secho(f"Erreur : PDF introuvable à l'emplacement {path}", fg="red")
        return

    init_db()
    db: Session = SessionLocal()

    try:
        # 1. Institution
        institution = db.query(Institution).filter(Institution.sigle == institution_sigle).first()
        if not institution:
            institution = Institution(sigle=institution_sigle, nom=f"Institution {institution_sigle}")
            db.add(institution)
            db.commit()
            db.refresh(institution)

        # 2. Document
        document = db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first()

        if document:
            click.secho(f"Avertissement : Aucun import effectué, document_key déjà existant ({document_key}).", fg="yellow")
            return

        pub_date = None
        if publication_date:
            pub_date = datetime.strptime(publication_date, "%Y-%m-%d").date()

        document = LegalDocument(
            document_key=document_key,
            type_code='CODE',
            institution_id=institution.id,
            stock_code=document_key,
            titre_officiel=title,
            document_role='STOCK',
            consolidation_as_of=datetime.utcnow().date(),
            date_publication=pub_date,
            extraction_status='processing'
        )
        db.add(document)
        db.commit()
        db.refresh(document)

        # 3. Fichier
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "documents", "pdfs")
        os.makedirs(upload_dir, exist_ok=True)
        filename = f"{int(datetime.utcnow().timestamp())}_{os.path.basename(path)}"
        dest_path = os.path.join(upload_dir, filename)

        shutil.copy2(path, dest_path)

        doc_file = MediaFile(
            document_id=document.id,
            file_path=dest_path,
            storage_provider="LOCAL",
            bucket_name="local-storage",
            object_key=f"storage/documents/pdfs/{filename}",
            original_filename=os.path.basename(path),
            mime_type="application/pdf",
            file_category="SOURCE_PDF",
            file_size=os.path.getsize(dest_path),
            checksum_sha256=compute_sha256(dest_path),
            description="PDF source importe via la CLI"
        )
        db.add(doc_file)
        db.flush()

        # 4. Extraction Run
        run = ExtractionRun(
            document_id=document.id,
            source="PARSING",
            source_media_file_id=doc_file.id,
            status="running"
        )
        db.add(run)
        db.commit()

        click.echo(f"Document créé: {document.id}")

        if sync:
            click.echo("Début de l'analyse du PDF avec PyMuPDF et spaCy...")
            parser = LegalDocumentParser(pdf_path=dest_path)
            hierarchy = parser.parse_hierarchy()

            from sqlalchemy_utils import Ltree

            seen_article_numbers = {}

            # Fonction pour insérer récursivement
            def insert_nodes(nodes_list, parent_tree_path=None, parent_node_id=None, start_order=0):
                current_order = start_order
                for node_data in nodes_list:
                    # Générer un ID UUID et sa version formatée pour ltree (sans tirets)
                    # Ltree labels must start with a letter. We prefix with 'n' (node)
                    node_id = uuid.uuid4()
                    node_ltree_id = f"n_{str(node_id).replace('-', '_')}"

                    current_tree_path = f"{parent_tree_path}.{node_ltree_id}" if parent_tree_path else node_ltree_id
                    ltree_obj = Ltree(current_tree_path)

                    if node_data["type"] == "ARTICLE":
                        num = str(node_data.get("number", "")).strip()
                        if not num:
                            num = "SANS_NUM_" + str(uuid.uuid4())[:8]

                        # Prévention du crash pour violation de contrainte unique "uq_articles_document_numero"
                        if num in seen_article_numbers:
                            seen_article_numbers[num] += 1
                            num = f"{num}_doublon_{seen_article_numbers[num]}"
                        else:
                            seen_article_numbers[num] = 0

                        # C'est un Article, on l'insère dans `articles` et `article_versions`
                        article = Article(
                            id=node_id,
                            document_id=document.id,
                            parent_node_id=parent_node_id,
                            numero_article=num,
                            ordre_affichage=current_order,
                            validation_status="pending"
                        )
                        db.add(article)

                        version = ArticleVersion(
                            article_id=article.id,
                            contenu_texte=node_data.get("content", ""),
                            validity_period=DateRange(datetime.utcnow().date(), None),
                            validation_status="pending"
                        )
                        db.add(version)
                    else:
                        # C'est un nœud structurel
                        node = StructureNode(
                            id=node_id,
                            document_id=document.id,
                            type_unite=node_data["type"],
                            numero=node_data.get("number", ""),
                            titre=node_data.get("title", ""),
                            tree_path=ltree_obj,
                            sort_order=current_order,
                            validation_status="pending"
                        )
                        db.add(node)
                        db.flush()

                        if node_data.get("children"):
                            insert_nodes(node_data["children"], parent_tree_path=current_tree_path, parent_node_id=node.id, start_order=0)

                    current_order += 1

            insert_nodes(hierarchy)

            run.status = "succeeded"
            run.finished_at = datetime.utcnow()
            document.extraction_status = "completed"
            db.commit()

            click.secho("Extraction terminée avec succès. La base de données PostgreSQL a été peuplée.", fg="green")

            nb_nodes = db.query(StructureNode).filter(StructureNode.document_id == document.id).count()
            nb_articles = db.query(Article).filter(Article.document_id == document.id).count()
            click.echo(f"Résumé : {nb_nodes} nœuds structurels et {nb_articles} articles extraits.")

    except Exception as e:
        db.rollback()
        click.secho(f"Erreur lors de l'exécution : {str(e)}", fg="red")
    finally:
        db.close()

if __name__ == "__main__":
    cli()
