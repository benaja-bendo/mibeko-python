import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import DATERANGE, JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy_utils import LtreeType

from src.db.database import Base


class Institution(Base):
    """Represente une institution emettrice de documents juridiques."""

    __tablename__ = "institutions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nom = Column(String(200), nullable=False)
    sigle = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = relationship("LegalDocument", back_populates="institution")


class OfficialJournal(Base):
    """Represente un Journal Officiel et sert de parent aux textes FLUX extraits."""

    __tablename__ = "official_journals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    publication_date = Column(Date, nullable=False)
    # Aligné sur media_files.file_path : le chemin S3 dérivé du titre (long) d'un
    # acte dépassait VARCHAR(255). Cf. migration widen_official_journals_file_path.
    file_path = Column(String(512), nullable=False)
    transcription_status = Column(String(255), nullable=True)
    is_published = Column(Boolean, default=False)
    number = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    legal_documents = relationship("LegalDocument", back_populates="official_journal")


class LegalDocument(Base):
    """Represente l'identite metier minimale d'un document legal."""

    __tablename__ = "legal_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type_code = Column(String(10), nullable=True)
    institution_id = Column(UUID(as_uuid=True), ForeignKey("institutions.id"), nullable=True)
    official_journal_id = Column(UUID(as_uuid=True), ForeignKey("official_journals.id"), nullable=True)
    document_key = Column(Text, nullable=True)
    document_role = Column(String(20), nullable=False, default="FLUX")
    consolidation_as_of = Column(Date, nullable=True)
    stock_code = Column(String(100), nullable=True)
    titre_officiel = Column(Text, nullable=False)
    # Objet de l'acte derive de son corps, jamais un titre officiel : sur les
    # « actes en abrege » du JO (nominations), le titre imprime se reduit au
    # type, au numero et a la date. Ecrit cote Laravel apres relecture humaine
    # (mibeko:proposer-libelles) ; le pipeline ne le renseigne JAMAIS — il est
    # mappe ici pour la lecture (panneau d'ingestion) et pour tenir la regle de
    # synchronisation des modeles avec les migrations Laravel.
    libelle_descriptif = Column(Text, nullable=True)
    libelle_descriptif_source = Column(String(20), nullable=True)
    reference_nor = Column(String(50), nullable=True)
    date_signature = Column(Date, nullable=True)
    date_publication = Column(Date, nullable=True)
    date_entree_vigueur = Column(Date, nullable=True)
    statut = Column(String(20), default="vigueur")
    legal_scope = Column(String(20), nullable=False, default="national")
    curation_status = Column(String(255), default="draft")
    extraction_status = Column(String(20), nullable=True)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    institution = relationship("Institution", back_populates="documents")
    official_journal = relationship("OfficialJournal", back_populates="legal_documents")
    files = relationship("MediaFile", back_populates="document", cascade="all, delete-orphan")
    extraction_runs = relationship("ExtractionRun", back_populates="document", cascade="all, delete-orphan")
    structure_nodes = relationship("StructureNode", back_populates="document", cascade="all, delete-orphan")
    articles = relationship("Article", back_populates="document", cascade="all, delete-orphan")


class MediaFile(Base):
    """Represente un artefact physique stocke dans MinIO/Domino."""

    __tablename__ = "media_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("legal_documents.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(512), nullable=False)
    storage_provider = Column(String(20), nullable=False, default="MINIO")
    bucket_name = Column(String(100), nullable=False, default="mibeko-documents")
    object_key = Column(String(512), nullable=False)
    original_filename = Column(String(255), nullable=True)
    mime_type = Column(String(100), default="application/pdf")
    file_category = Column(String(50), nullable=False, default="SOURCE_PDF")
    file_size = Column(BigInteger, nullable=True)
    # Nombre de pages d'un PDF source. Nullable : renseigne a l'ingestion pour
    # les nouveaux fichiers, par rattrapage pour les anciens. Sert au controle
    # des reperes de page du dossier de travail cote Laravel, qui se tait tant
    # que la valeur est inconnue (migration 2026_08_29_140000).
    page_count = Column(Integer, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    document = relationship("LegalDocument", back_populates="files")


class ExtractionRun(Base):
    """Trace une execution d'extraction ou de parsing et ses artefacts lies."""

    __tablename__ = "extraction_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("legal_documents.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False, default="MINERU")
    status = Column(String(20), nullable=False, default="queued")
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    source_media_file_id = Column(UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True)
    markdown_media_file_id = Column(UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True)
    json_media_file_id = Column(UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True)
    meta = Column(JSONB, default=dict)

    document = relationship("LegalDocument", back_populates="extraction_runs")


class StructureNode(Base):
    """Represente un noeud structurel dans la hierarchie d'un document."""

    __tablename__ = "structure_nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("legal_documents.id", ondelete="CASCADE"), nullable=False)
    type_unite = Column(String(50), nullable=False)
    numero = Column(String(50), nullable=True)
    titre = Column(Text, nullable=True)
    tree_path = Column(LtreeType, nullable=False)
    validation_status = Column(String(255), default="pending")
    sort_order = Column(Integer, default=0)
    # SoftDeletes côté Laravel : une reconstruction publiée retire les anciennes
    # divisions sans perdre leurs flags, chemins ni capacité de restauration.
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    document = relationship("LegalDocument", back_populates="structure_nodes")
    articles = relationship("Article", back_populates="parent_node")


class Article(Base):
    """Represente l'identite stable d'un article dans un document."""

    __tablename__ = "articles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("legal_documents.id", ondelete="CASCADE"), nullable=False)
    parent_node_id = Column(UUID(as_uuid=True), ForeignKey("structure_nodes.id"), nullable=True)
    numero_article = Column(String(50), nullable=False)
    ordre_affichage = Column(Integer, default=0)
    validation_status = Column(String(20), default="pending")
    # SoftDeletes côté Laravel : les lectures Python doivent filtrer deleted_at IS
    # NULL pour ne pas compter/exposer des articles supprimés par la curation.
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    document = relationship("LegalDocument", back_populates="articles")
    parent_node = relationship("StructureNode", back_populates="articles")
    versions = relationship("ArticleVersion", back_populates="article", cascade="all, delete-orphan")


class CurationFlag(Base):
    """Signale une anomalie à relire avant publication (trou/doublon d'articles…)."""

    __tablename__ = "curation_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("legal_documents.id"), nullable=True)
    article_id = Column(UUID(as_uuid=True), ForeignKey("articles.id"), nullable=True)
    # Origine du signalement : la couche heuristique (numérotation) tague
    # 'heuristic' pour pouvoir être purgée/recréée sans toucher aux flags
    # humains ni aux autres couches (structural/llm) côté Laravel. Le défaut
    # DB est 'human' (flags créés depuis l'éditeur) : divergence VOULUE.
    source = Column(String(20), nullable=False, default="heuristic")
    type_probleme = Column(String(50), nullable=True)
    # Seul 'blocking' empêche la publication côté Laravel (warning/info informent).
    severity = Column(String(20), nullable=False, default="blocking")
    description = Column(Text, nullable=True)
    resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ArticleVersion(Base):
    """Represente une version textuelle datee d'un article."""

    __tablename__ = "article_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(UUID(as_uuid=True), ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    contenu_texte = Column(Text, nullable=False)
    validity_period = Column(DATERANGE, nullable=False)
    source_run_id = Column(UUID(as_uuid=True), ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True)
    source_media_file_id = Column(UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True)
    source_locator = Column(JSONB, default=dict)
    validation_status = Column(String(255), default="pending")

    article = relationship("Article", back_populates="versions")
