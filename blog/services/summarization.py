import json
import re
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from blog.models import Article


class ArticleSummarizationService:
    ALLOWED_CATEGORIES = ("World", "Tech", "Sport", "Others")

    def _fallback_summary(self, text: str, max_words: int = 80) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "..."

    def _infer_category_from_text(self, text: str) -> str:
        text_blob = (text or "").lower()
        sport_terms = ("sport", "football", "soccer", "league", "match", "nba", "nfl", "cricket", "tennis")
        tech_terms = ("tech", "ai", "software", "chip", "cyber", "cloud", "startup", "apple", "google", "microsoft")
        world_terms = ("world", "government", "election", "war", "policy", "diplom", "country", "global")

        if any(term in text_blob for term in sport_terms):
            return "Sport"
        if any(term in text_blob for term in tech_terms):
            return "Tech"
        if any(term in text_blob for term in world_terms):
            return "World"
        return "Others"

    def _normalize_category(self, category: str) -> str:
        value = (category or "").strip().lower()
        mapping = {
            "world": "World",
            "tech": "Tech",
            "technology": "Tech",
            "sport": "Sport",
            "sports": "Sport",
            "others": "Others",
            "other": "Others",
        }
        return mapping.get(value, "Others")

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))

    def _build_prompt(self, text: str, mode: str) -> str:
        if mode == "deep":
            return (
                "You are a news summarizer and lightweight category classifier. "
                "Return ONLY valid JSON with this exact schema: "
                '{"summary":"...","category":"World|Tech|Sport|Others"}. '
                "For summary: provide 5 bullets and one short context paragraph, factual only, no speculation.\n\n"
                f"Article:\n{text}"
            )
        return (
            "You are a news summarizer and lightweight category classifier. "
            "Return ONLY valid JSON with this exact schema: "
            '{"summary":"...","category":"World|Tech|Sport|Others"}. '
            "For summary: write one concise factual paragraph with key points.\n\n"
            f"Article:\n{text}"
        )

    def _extract_json_candidate(self, raw_text: str) -> str:
        text = (raw_text or "").strip()
        if not text:
            return ""

        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fenced:
            return (fenced.group(1) or "").strip()

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]
        return text

    def _parse_structured_response(self, raw_text: str, source_text: str) -> tuple[str, str]:
        candidate = self._extract_json_candidate(raw_text)
        try:
            payload = json.loads(candidate)
            summary = (payload.get("summary") or "").strip()
            category = self._normalize_category(payload.get("category") or "")
            if summary:
                return summary, category
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        summary = (raw_text or "").strip() or self._fallback_summary(source_text)
        return summary, self._infer_category_from_text(source_text)

    def _gemini_api_keys(self) -> list[str]:
        configured = getattr(settings, "GEMINI_API_KEYS", "")
        keys = [item.strip() for item in configured.split(",") if item.strip()]
        primary = (getattr(settings, "GEMINI_API_KEY", "") or "").strip()
        if primary and primary not in keys:
            keys.append(primary)
        return keys

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
        api_keys = self._gemini_api_keys()
        if not api_keys:
            return "", {}

        model = getattr(settings, "GEMINI_MODEL", "gemini-2.0-flash")
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 220},
        }
        for key_index, api_key in enumerate(api_keys, start=1):
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
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
                continue

            candidates = data.get("candidates", [])
            if not candidates:
                continue

            parts = candidates[0].get("content", {}).get("parts", [])
            raw_summary = "\n".join(part.get("text", "") for part in parts if part.get("text")).strip()
            if not raw_summary:
                continue
            summary, category = self._parse_structured_response(raw_summary, prompt)
            usage = data.get("usageMetadata", {})
            return summary, {
                "provider": "gemini",
                "model": model,
                "category": category,
                "gemini_key_slot": key_index,
                "gemini_keys_tried": key_index,
                "prompt_tokens": int(usage.get("promptTokenCount") or 0),
                "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
                "total_tokens": int(usage.get("totalTokenCount") or 0),
            }

        return "", {}

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

        raw_summary = (choices[0].get("message", {}).get("content") or "").strip()
        if not raw_summary:
            return "", {}
        summary, category = self._parse_structured_response(raw_summary, prompt)
        usage = data.get("usage", {})
        return summary, {
            "provider": "groq",
            "model": model,
            "category": category,
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
                meta["category"] = self._normalize_category(meta.get("category", "Others"))
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
            "category": self._infer_category_from_text(text),
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
        article.summary_category = self._normalize_category(meta.get("category", "Others"))
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
                "summary_category",
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
