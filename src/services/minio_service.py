import os
from minio import Minio
from minio.error import S3Error
from dotenv import load_dotenv

load_dotenv()

MINIO_HOST = os.getenv("MINIO_HOST", "127.0.0.1")
MINIO_PORT = os.getenv("MINIO_PORT", "9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "root")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "password")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

class MinioService:
    def __init__(self):
        self.client = Minio(
            f"{MINIO_HOST}:{MINIO_PORT}",
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE
        )
        self.bucket_name = "mibeko-documents"
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self):
        try:
            if not self.client.bucket_exists(self.bucket_name):
                self.client.make_bucket(self.bucket_name)
        except S3Error as err:
            print(f"Erreur MinIO: {err}")

    def upload_file(self, object_name: str, file_path: str, content_type: str = "application/pdf"):
        """Upload un fichier local vers MinIO."""
        try:
            self.client.fput_object(
                self.bucket_name,
                object_name,
                file_path,
                content_type=content_type
            )
            return f"s3://{self.bucket_name}/{object_name}"
        except S3Error as err:
            print(f"Erreur d'upload MinIO: {err}")
            return None

    def upload_bytes(self, object_name: str, data: bytes, content_type: str = "application/pdf"):
        """Upload des données en mémoire vers MinIO."""
        import io
        try:
            data_stream = io.BytesIO(data)
            self.client.put_object(
                self.bucket_name,
                object_name,
                data_stream,
                length=len(data),
                content_type=content_type
            )
            return f"s3://{self.bucket_name}/{object_name}"
        except S3Error as err:
            print(f"Erreur d'upload MinIO (bytes): {err}")
            return None

    def get_file_url(self, object_name: str) -> str:
        """Génère une URL signée pour accéder au fichier."""
        try:
            from datetime import timedelta
            return self.client.get_presigned_url(
                "GET",
                self.bucket_name,
                object_name,
                expires=timedelta(days=1)
            )
        except S3Error as err:
            print(f"Erreur URL MinIO: {err}")
            return ""

    def get_file_bytes(self, object_name: str) -> bytes:
        """Télécharge le fichier depuis MinIO en mémoire."""
        try:
            response = self.client.get_object(self.bucket_name, object_name)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as err:
            print(f"Erreur téléchargement MinIO: {err}")
            return None

minio_service = MinioService()
