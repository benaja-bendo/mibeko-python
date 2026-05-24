import fitz  # PyMuPDF
import re
import logging
from typing import List, Dict, Any

_nlp = None

def get_nlp():
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("fr_core_news_sm")
        except Exception as e:
            logging.warning(f"Impossible de charger le modèle spaCy 'fr_core_news_sm' : {e}. Utilisation du fallback en texte brut.")
            _nlp = False
    return _nlp

class LegalDocumentParser:
    """
    Parseur pour extraire la structure hiérarchique d'un code juridique (ex: Code du Travail)
    depuis un texte brut ou un PDF en utilisant des expressions régulières.
    """
    
    def __init__(self, pdf_path: str = None, text_content: str = None):
        self.pdf_path = pdf_path
        self.text_content = text_content
        # Expressions régulières pour identifier les niveaux hiérarchiques
        # Adapté au format habituel des textes de loi francophones et aux sorties Markdown
        self.patterns = {
            "LIVRE": re.compile(r"^(?:#+\s*)?(?:LIVRE|Livre)\s+([A-Z]+|\d+)(?:[\.\-\s:]+(.*))?$", re.IGNORECASE),
            "TITRE": re.compile(r"^(?:#+\s*)?(?:TITRE|Titre)\s+([A-Z]+|\d+)(?:[\.\-\s:]+(.*))?$", re.IGNORECASE),
            "CHAPITRE": re.compile(r"^(?:#+\s*)?(?:CHAPITRE|Chapitre)\s+([A-Z]+|\d+)(?:[\.\-\s:]+(.*))?$", re.IGNORECASE),
            "SECTION": re.compile(r"^(?:#+\s*)?(?:SECTION|Section)\s+(\d+|[A-Z]+|PREMI[EÈ]RE|UNIQUE)(?:[\.\-\s:]+(.*))?$", re.IGNORECASE),
            "ARTICLE": re.compile(r"^(?:#+\s*)?(?:Article|Art\.?)\s+([LDRA]?\.?\s*\d+[a-zA-Z0-9\-]*(?:\s+bis|\s+ter|\s+quater|\s+er)?)\.?\s*(.*)$", re.IGNORECASE)
        }
        
    def extract_text(self) -> str:
        """
        Retourne le texte fourni ou l'extrait du PDF en utilisant PyMuPDF.
        """
        if self.text_content:
            return self.text_content
            
        if not self.pdf_path:
            return ""
            
        doc = fitz.open(self.pdf_path)
        full_text = []
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            # Utilisation de l'OCR pour les PDF scannés
            try:
                tp = page.get_textpage_ocr(flags=0, dpi=300, full=True, language='fra')
                text = tp.extractText()
            except Exception:
                text = page.get_text("text")
                
            # Nettoyage simple
            lines = text.split("\n")
            clean_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                # Ignorer les numéros de page isolés
                if stripped.isdigit() and len(stripped) < 4:
                    continue
                clean_lines.append(stripped)
                
            full_text.append("\n".join(clean_lines))
            
        return "\n".join(full_text)

    def parse_hierarchy(self) -> List[Dict[str, Any]]:
        """
        Analyse le texte brut et reconstruit l'arborescence du document.
        Retourne une liste de noeuds racines (généralement des LIVREs ou TITREs).
        """
        text = self.extract_text()
        lines = text.split("\n")
        
        nodes = []
        # Pile pour maintenir le contexte hiérarchique actuel
        # Index 0: LIVRE, 1: TITRE, 2: CHAPITRE, 3: SECTION, 4: ARTICLE
        stack = {
            "LIVRE": None,
            "TITRE": None,
            "CHAPITRE": None,
            "SECTION": None,
            "ARTICLE": None
        }
        
        current_article_content = []
        
        def save_current_article():
            """Sauvegarde le contenu de l'article en cours d'analyse."""
            if stack["ARTICLE"] and current_article_content:
                raw_content = " ".join(current_article_content)
                nlp_model = get_nlp()
                if nlp_model:
                    try:
                        doc = nlp_model(raw_content)
                        clean_content = " ".join([sent.text for sent in doc.sents])
                    except Exception as e:
                        logging.warning(f"Erreur lors du traitement NLP d'un article : {e}")
                        clean_content = raw_content
                else:
                    clean_content = raw_content
                stack["ARTICLE"]["content"] = clean_content
                current_article_content.clear()

        def add_node(node_type, number, title):
            save_current_article()
            
            node = {
                "type": node_type,
                "number": number.strip() if number else "",
                "title": title.strip() if title else "",
                "content": "",
                "children": []
            }
            
            # Déterminer où attacher ce noeud
            if node_type == "LIVRE":
                nodes.append(node)
                stack["LIVRE"] = node
                stack["TITRE"] = stack["CHAPITRE"] = stack["SECTION"] = stack["ARTICLE"] = None
                
            elif node_type == "TITRE":
                if stack["LIVRE"]:
                    stack["LIVRE"]["children"].append(node)
                else:
                    nodes.append(node)
                stack["TITRE"] = node
                stack["CHAPITRE"] = stack["SECTION"] = stack["ARTICLE"] = None
                
            elif node_type == "CHAPITRE":
                if stack["TITRE"]:
                    stack["TITRE"]["children"].append(node)
                elif stack["LIVRE"]:
                    stack["LIVRE"]["children"].append(node)
                else:
                    nodes.append(node)
                stack["CHAPITRE"] = node
                stack["SECTION"] = stack["ARTICLE"] = None
                
            elif node_type == "SECTION":
                if stack["CHAPITRE"]:
                    stack["CHAPITRE"]["children"].append(node)
                elif stack["TITRE"]:
                    stack["TITRE"]["children"].append(node)
                else:
                    nodes.append(node)
                stack["SECTION"] = node
                stack["ARTICLE"] = None
                
            elif node_type == "ARTICLE":
                parent = stack["SECTION"] or stack["CHAPITRE"] or stack["TITRE"] or stack["LIVRE"]
                if parent:
                    parent["children"].append(node)
                else:
                    nodes.append(node)
                stack["ARTICLE"] = node

        for line in lines:
            matched = False
            for node_type, pattern in self.patterns.items():
                match = pattern.match(line)
                if match:
                    number = match.group(1)
                    title = match.group(2) if len(match.groups()) > 1 else ""
                    add_node(node_type, number, title)
                    matched = True
                    break
            
            if not matched:
                # Si ce n'est pas un titre, c'est potentiellement le contenu d'un article ou le titre sur la ligne suivante
                if stack["ARTICLE"]:
                    current_article_content.append(line)
                else:
                    # On pourrait être face au titre d'un Livre/Titre/Chapitre écrit sur la ligne suivante
                    # Simplification : on l'ajoute au titre du dernier élément ouvert (qui n'est pas un article)
                    for t in ["SECTION", "CHAPITRE", "TITRE", "LIVRE"]:
                        if stack[t] and not stack[t]["title"]:
                            stack[t]["title"] = line
                            break

        # Sauvegarder le dernier article
        save_current_article()
        
        return nodes
