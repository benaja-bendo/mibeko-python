import os
import json
import uuid
import httpx
import time
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Backend d'extraction :
#   cloud  → API SaaS MinerU (https://mineru.net/api/v4), asynchrone (défaut).
#   local  → serveur mineru-api auto-hébergé (cf. mineru-local/), synchrone.
MINERU_BACKEND = os.getenv("MINERU_BACKEND", "cloud").strip().lower()

MINERU_API_URL = os.getenv("MINERU_API_URL", "https://mineru.net/api/v4")
MINERU_API_KEY = os.getenv("MINERU_API_KEY", "")
# Langue OCR pour le backend local (textes juridiques congolais = français).
MINERU_LANG = os.getenv("MINERU_LANG", "fr")

# Délai maximal (s) pour UNE requête /file_parse au backend local (synchrone :
# la réponse HTTP n'arrive qu'une fois tout le PDF traité). 900s (15 min)
# s'est révélé insuffisant en pratique sur CPU émulé (Docker/Mac) : un JO
# scanné de 25 pages a dépassé ce délai alors que le serveur travaillait
# toujours dessus (vérifié le 04/07/2026 — /health montrait processing_tasks
# encore actif après l'abandon côté client). 45 min par défaut, ajustable.
MINERU_LOCAL_TIMEOUT_SECONDS = float(os.getenv("MINERU_LOCAL_TIMEOUT_SECONDS", "2700"))

# Le pipeline OCR local (PP-OCRv5) n'expose pas de modèle « latin/français » ;
# son modèle par défaut (« ch ») reconnaît déjà l'écriture latine. Pour ces
# langues on OMET lang_list — sinon l'API renvoie 400 « Language not supported ».
_LATIN_LANGS = {"fr", "en", "latin", "es", "de", "it", "pt", "ca", "nl"}

# Préfixe des « URLs » retournées par le backend local : le contenu (.md/.json)
# est déjà en mémoire, on évite tout aller-retour réseau ou fichier temporaire.
_MEM_SCHEME = "mem://"


class MineruService:
    def __init__(self):
        self.api_url = MINERU_API_URL
        self.backend = MINERU_BACKEND
        self.headers = {
            "Authorization": f"Bearer {MINERU_API_KEY}",
            "Accept": "application/json"
        }
        # Résultats du backend local en attente de récupération (token -> contenu).
        self._local_results: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    #  Backend LOCAL (serveur mineru-api auto-hébergé, endpoint /file_parse)
    # ------------------------------------------------------------------ #

    async def _submit_local(self, file_path: str) -> str:
        """Parse un PDF en synchrone via mineru-api local et stocke le résultat.

        Le backend par défaut du serveur exige un GPU : on impose `pipeline`
        (modèles CPU). On demande le markdown ET le `middle_json` (schéma
        `pdf_info[].para_blocks` attendu par le parseur Mibeko).
        """
        endpoint = f"{self.api_url}/file_parse"
        # FastAPI attend des champs de formulaire ; une valeur unique pour
        # `lang_list` est coercée en liste à 1 élément côté serveur.
        data = {
            "backend": "pipeline",
            "parse_method": "auto",
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "false",
            "return_model_output": "false",
            "return_images": "false",
            "response_format_zip": "false",
        }
        # Langue OCR : omise pour le latin (modèle « ch » par défaut), sinon
        # transmise telle quelle (ex. cyrillic, arabic…).
        if MINERU_LANG and MINERU_LANG.strip().lower() not in _LATIN_LANGS:
            data["lang_list"] = MINERU_LANG.strip()

        # Le parsing CPU peut être long (plusieurs minutes pour un gros JO).
        async with httpx.AsyncClient(timeout=httpx.Timeout(MINERU_LOCAL_TIMEOUT_SECONDS)) as client:
            with open(file_path, "rb") as f:
                files = {"files": (os.path.basename(file_path), f, "application/pdf")}
                response = await client.post(endpoint, data=data, files=files)

        response.raise_for_status()
        payload = response.json()

        # results = { "<nom_fichier>": { md_content, middle_json, ... } }
        results = payload.get("results") or {}
        first = next(iter(results.values()), {}) if results else {}

        # middle_json peut arriver en chaîne JSON ou déjà désérialisé : on
        # garantit une chaîne (le parseur fait json.loads ensuite).
        middle = first.get("middle_json") or ""
        if middle and not isinstance(middle, str):
            middle = json.dumps(middle, ensure_ascii=False)

        token = uuid.uuid4().hex
        self._local_results[token] = {
            "md": first.get("md_content") or "",
            "json": middle,
            "status": payload.get("status", "success"),
        }
        return token

    def _get_results_local(self, token: str) -> dict:
        """Expose le résultat local sous la même forme que le backend cloud."""
        stored = self._local_results.get(token)
        if stored is None:
            return {"status": "failed", "error": "Résultat local introuvable."}

        result: dict = {"status": "success"}
        if stored.get("md"):
            result["md_url"] = f"{_MEM_SCHEME}{token}/md"
        if stored.get("json"):
            result["json_url"] = f"{_MEM_SCHEME}{token}/json"
        return result

    def _download_local(self, url: str) -> bytes:
        """Résout une « URL » mémoire `mem://<token>/<kind>` en octets."""
        ref = url[len(_MEM_SCHEME):]
        token, _, kind = ref.partition("/")
        stored = self._local_results.get(token, {})
        content = stored.get(kind, "")
        data = content.encode("utf-8")

        # Libère la mémoire une fois les deux artefacts (md + json) consommés.
        if isinstance(stored, dict):
            stored.pop(kind, None)
            if not stored.get("md") and not stored.get("json"):
                self._local_results.pop(token, None)

        return data

    # ------------------------------------------------------------------ #
    #  API publique (dispatch cloud / local — signatures inchangées)
    # ------------------------------------------------------------------ #

    async def submit_pdf(self, file_path: str) -> str:
        """
        Soumet un PDF à l'API MinerU pour extraction.
        Retourne le task_id ou soulève une exception.
        """
        if self.backend == "local":
            return await self._submit_local(file_path)

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
        if self.backend == "local":
            # Le parsing local est déjà terminé (synchrone) : pas d'attente.
            return self._get_results_local(task_id)

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
        if url.startswith(_MEM_SCHEME):
            return self._download_local(url)

        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            return response.content

mineru_service = MineruService()
