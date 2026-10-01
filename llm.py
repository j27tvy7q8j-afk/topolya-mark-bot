"""Провайдер модели. Сейчас — OpenAI-совместимый API Yandex AI Studio."""
import asyncio
import logging

import httpx

import config

log = logging.getLogger("llm")


class LLMError(Exception):
    pass


class OpenAICompat:
    def __init__(self, base_url, api_key, folder_id, name="yandex"):
        self.base_url, self.api_key, self.folder, self.name = base_url, api_key, folder_id, name

    def model_uri(self, model):
        return model if "://" in model else f"gpt://{self.folder}/{model}"

    async def chat(self, model, messages, temperature=0.2, max_tokens=1500, timeout=90):
        payload = {"model": self.model_uri(model), "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Authorization": f"Api-Key {self.api_key}", "x-folder-id": self.folder,
                   "Content-Type": "application/json"}
        last = LLMError("нет ответа")
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            except httpx.HTTPError as e:
                last = LLMError(f"сеть: {e}")
            else:
                if r.status_code == 200:
                    content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
                    if content and content.strip():
                        return content.strip()
                    last = LLMError("пустой ответ модели")
                elif r.status_code in (429, 500, 502, 503, 504):
                    last = LLMError(f"HTTP {r.status_code}")
                else:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            await asyncio.sleep(1.5 * (attempt + 1))
        raise last


def get_provider():
    return OpenAICompat(config.LLM_BASE_URL, config.YANDEX_API_KEY, config.YANDEX_FOLDER_ID)
