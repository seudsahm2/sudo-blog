import json
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from blog.models import Article


class ArticleSummarizationService:
    def _fallback_summary(self, text: str, max_words: int = 80) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "..."

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))

    def _build_prompt(self, text: str, mode: str) -> str:
        if mode == "deep":
            return (
                "Summarize this news article in 5 bullets, then add one-paragraph context and key implications."
                " Keep facts strict and avoid speculation.\n\n"
                f"Article:\n{text}"
            )
        return (
            "Summarize this news article in a concise factual paragraph with key points.\n\n"
            f"Article:\n{text}"
        )

    def _provider_order(self) -> list[str]:
        preferred = getattr(settings, "AI_SUMMARY_PROVIDER", "gemini").lower().strip()
        if preferred == "groq":
            return ["groq", "gemini"]
        if preferred == "gemini":
            return ["gemini", "groq"]
        return ["gemini", "groq"]

    def _compute_cost(self, provider: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
        if provider == "gemini":
            input_rate = Decimal(str(getattr(settings, "GEMINI_INPUT_COST_PER_1K", "0")))
            output_rate = Decimal(str(getattr(settings, "GEMINI_OUTPUT_COST_PER_1K", "0")))
        elif provider == "groq":
            input_rate = Decimal(str(getattr(settings, "GROQ_INPUT_COST_PER_1K", "0")))
            output_rate = Decimal(str(getattr(settings, "GROQ_OUTPUT_COST_PER_1K", "0")))
        else:
            return Decimal("0")

        return (Decimal(prompt_tokens) / Decimal(1000) * input_rate) + (
            Decimal(completion_tokens) / Decimal(1000) * output_rate
        )

    def _summarize_with_gemini(self, prompt: str) -> tuple[str, dict]:
        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            return "", {}

        model = getattr(settings, "GEMINI_MODEL", "gemini-2.0-flash")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 220},
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError):
            return "", {}

        candidates = data.get("candidates", [])
        if not candidates:
            return "", {}

        parts = candidates[0].get("content", {}).get("parts", [])
        summary = "\n".join(part.get("text", "") for part in parts if part.get("text")).strip()
        usage = data.get("usageMetadata", {})
        return summary, {
            "provider": "gemini",
            "model": model,
            "prompt_tokens": int(usage.get("promptTokenCount") or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
            "total_tokens": int(usage.get("totalTokenCount") or 0),
        }

    def _summarize_with_groq(self, prompt: str) -> tuple[str, dict]:
        api_key = getattr(settings, "GROQ_API_KEY", "")
        if not api_key:
            return "", {}

        model = getattr(settings, "GROQ_MODEL", "llama-3.3-70b-versatile")
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You summarize news with factual precision."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 220,
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError):
            return "", {}

        choices = data.get("choices", [])
        if not choices:
            return "", {}

        summary = (choices[0].get("message", {}).get("content") or "").strip()
        usage = data.get("usage", {})
        return summary, {
            "provider": "groq",
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    def summarize_text(self, text: str) -> tuple[str, dict]:
        mode = getattr(settings, "SUMMARIZER_PROMPT_MODE", "brief").lower().strip()
        mode = "deep" if mode == "deep" else "brief"
        prompt = self._build_prompt(text, mode)

        for provider in self._provider_order():
            if provider == "gemini":
                summary, meta = self._summarize_with_gemini(prompt)
            else:
                summary, meta = self._summarize_with_groq(prompt)

            if summary:
                prompt_tokens = meta.get("prompt_tokens") or self._estimate_tokens(prompt)
                completion_tokens = meta.get("completion_tokens") or self._estimate_tokens(summary)
                total_tokens = meta.get("total_tokens") or (prompt_tokens + completion_tokens)
                meta["prompt_tokens"] = int(prompt_tokens)
                meta["completion_tokens"] = int(completion_tokens)
                meta["total_tokens"] = int(total_tokens)
                meta["prompt_mode"] = mode
                meta["estimated_cost_usd"] = str(
                    self._compute_cost(provider, int(prompt_tokens), int(completion_tokens))
                )
                return summary, meta

        summary = self._fallback_summary(text)
        prompt_tokens = self._estimate_tokens(prompt)
        completion_tokens = self._estimate_tokens(summary)
        return summary, {
            "provider": "fallback",
            "model": "extractive",
            "prompt_mode": mode,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated_cost_usd": "0",
        }

    def summarize_article(self, article: Article) -> Article:
        summary, meta = self.summarize_text(article.body)
        article.summary = summary
        article.summary_provider = meta.get("provider", "")
        article.summary_model = meta.get("model", "")
        article.summary_prompt_mode = meta.get("prompt_mode", "brief")
        article.summary_prompt_tokens = int(meta.get("prompt_tokens", 0))
        article.summary_completion_tokens = int(meta.get("completion_tokens", 0))
        article.summary_total_tokens = int(meta.get("total_tokens", 0))
        article.summary_estimated_cost_usd = Decimal(str(meta.get("estimated_cost_usd", "0")))
        article.status = Article.Status.SUMMARIZED
        article.save(
            update_fields=[
                "summary",
                "summary_provider",
                "summary_model",
                "summary_prompt_mode",
                "summary_prompt_tokens",
                "summary_completion_tokens",
                "summary_total_tokens",
                "summary_estimated_cost_usd",
                "status",
                "updated",
            ]
        )
        return article
