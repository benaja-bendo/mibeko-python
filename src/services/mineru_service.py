import os
import httpx
import time
import asyncio
from dotenv import load_dotenv

load_dotenv()

MINERU_API_URL = os.getenv("MINERU_API_URL", "https://mineru.net/api/v4")
MINERU_API_KEY = os.getenv("MINERU_API_KEY", "")

class MineruService:
    def __init__(self):
        self.api_url = MINERU_API_URL
        self.headers = {
            "Authorization": f"Bearer {MINERU_API_KEY}",
            "Accept": "application/json"
        }

    async def submit_pdf(self, file_path: str) -> str:
        """
        Soumet un PDF à l'API MinerU pour extraction.
        Retourne le task_id ou soulève une exception.
        """
        async with httpx.AsyncClient() as client:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "application/pdf")}
                # Endpoint fictif standard, adapter selon la doc exacte de MinerU v4
                response = await client.post(
                    f"{self.api_url}/extract",
                    headers=self.headers,
                    files=files,
                    timeout=60.0
                )
                
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("task_id", "")

    async def check_status(self, task_id: str) -> dict:
        """
        Vérifie le statut de la tâche.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.api_url}/extract/{task_id}",
                headers=self.headers,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json().get("data", {})

    async def get_results(self, task_id: str):
        """
        Attend la fin de la tâche et récupère les URLs ou contenus .md et .json.
        """
        max_retries = 30
        retry_count = 0
        
        while retry_count < max_retries:
            status_data = await self.check_status(task_id)
            status = status_data.get("status")
            
            if status == "success" or status == "completed":
                # Récupérer les liens de téléchargement
                return {
                    "md_url": status_data.get("md_url"),
                    "json_url": status_data.get("json_url"),
                    "status": "success"
                }
            elif status == "failed":
                return {"status": "failed", "error": status_data.get("error", "Erreur inconnue")}
                
            retry_count += 1
            await asyncio.sleep(10) # Attendre 10 secondes avant de réessayer
            
        return {"status": "timeout", "error": "Le délai de traitement a expiré."}

    async def download_result(self, url: str) -> bytes:
        """Télécharge le fichier de résultat depuis l'URL fournie par MinerU."""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            return response.content

mineru_service = MineruService()
