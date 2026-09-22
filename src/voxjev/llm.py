"""Petit LLM via OpenRouter (clé OPENROUTER_API_KEY) : réponses courtes et brouillons d'e-mail.

Le texte produit ici n'est JAMAIS exécuté : il est affiché, lu à voix haute, ou placé dans un
brouillon que vous relisez et envoyez vous-même.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "inception/mercury-2.5"

ASK_PROMPT = (
    "Tu es l'assistant vocal d'un Mac. Réponds en français, en une à trois phrases courtes, sans "
    "markdown ni liste, comme si tu parlais. Si tu ne sais pas, dis-le simplement."
)
MAIL_PROMPT = (
    "Rédige un e-mail en français à partir de la consigne dictée. Réponds UNIQUEMENT par un objet JSON "
    '{"subject": "...", "body": "..."}. Ton simple et poli, pas de signature inventée, pas de '
    "coordonnées inventées. Le corps doit être court."
)


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def chat(system: str, user: str, *, model: str | None = None, max_tokens: int = 300, timeout: float = 12.0,
         json_mode: bool = False) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("clé OPENROUTER_API_KEY absente de .env")
    body = {
        "model": model or os.environ.get("VOXJEV_LLM_MODEL", DEFAULT_MODEL),
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "voxjev"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"LLM indisponible : {exc}") from exc


def ask(question: str) -> str:
    return chat(ASK_PROMPT, question, max_tokens=220)


def draft_mail(instruction: str) -> tuple[str, str]:
    raw = chat(MAIL_PROMPT, instruction, max_tokens=500, json_mode=True)
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        return str(data.get("subject", ""))[:200], str(data.get("body", ""))[:4000]
    except ValueError as exc:
        raise LLMError("brouillon illisible") from exc
