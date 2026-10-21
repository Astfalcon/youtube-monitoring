"""Google Gemini summarization module using the google-genai SDK."""

import os
import time
from typing import List, Tuple, Optional

from dotenv import load_dotenv

load_dotenv()


def _read_secret(name: str, default: str = "") -> str:
    """Try environment variables, then st.secrets; ignore unavailable Streamlit."""
    val = os.getenv(name, "")
    if val:
        return val
    try:
        import streamlit as st  # type: ignore
        return st.secrets.get(name, default) or default
    except Exception:
        return default


GEMINI_API_KEY = _read_secret("GOOGLE_API_KEY") or _read_secret("GEMINI_API_KEY")
GEMINI_MODEL = _read_secret("GEMINI_MODEL", "gemini-3.5-flash")


class SummarizerError(Exception):
    pass


def _get_client():
    if not GEMINI_API_KEY:
        raise SummarizerError(
            "GOOGLE_API_KEY is not set. Add it to your .env file."
        )
    try:
        from google import genai
    except ImportError as e:
        raise SummarizerError(
            "The google-genai package is required. Install it with `pip install google-genai`."
        ) from e

    return genai.Client(api_key=GEMINI_API_KEY)


VIDEO_SUMMARY_PROMPT = """The following is a transcript from a YouTube video. Summarize it in English using this structure.

**Title:** {title}

**Transcript:**
{transcript}

---

Summary format:
1. **One-sentence summary**
2. **Key points** (3-5 bullets)
3. **Main insights and implications** (2-3 sentences)
4. **Notable people, services, and keywords mentioned** (if any)

Important:
- Do not invent information that is not in the transcript.
- Exclude advertising and sponsorship messages.
- Use readable Markdown.
"""


TREND_SUMMARY_PROMPT = """The following is a list of long-form videos uploaded to several YouTube channels during a specific period.
Analyze and summarize them in English from an industry trends perspective.

Video list:
{video_list}

---

Analysis format:
1. **Key trends during this period** (3-5 items)
2. **Recurring topics and keywords**
3. **Differences in channel positioning** (if notable)
4. **Developments to watch next week** (predictions)

Important:
- Base the analysis only on the supplied data. Clearly label any speculation.
- Use Markdown.
"""


MAX_TRANSCRIPT_CHARS = 60000
TREND_MAX_VIDEOS = 60  # Maximum number of recent videos used for trend analysis.

# Fallback order for HTTP 503/429 errors, prioritizing models verified in 2026-05.
FALLBACK_MODELS = [
    GEMINI_MODEL,                # Primary model (gemini-3.5-flash by default).
    "gemini-3.1-flash-lite",     # Newer, fast, and reliable.
    "gemini-3-flash-preview",    # Newer preview model.
    "gemini-2.5-flash",          # Verified stable model.
    "gemini-2.5-flash-lite",     # Low-cost final fallback.
]


def _call_gemini_with_retry(client, prompt: str, max_attempts: int = 3) -> Tuple[str, str]:
    """Call Gemini, retry HTTP 503/429 errors with backoff, and fall back across models.
    Return the response text and the model name used.
    """
    last_err = None
    tried_models = []

    for model_name in FALLBACK_MODELS:
        if model_name in tried_models or not model_name:
            continue
        tried_models.append(model_name)

        for attempt in range(max_attempts):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = _extract_text(response)
                if text:
                    return text.strip(), model_name
                last_err = SummarizerError("Gemini returned an empty response.")
                break  # Retrying an empty response is not useful; try the next model.
            except Exception as e:
                msg = str(e)
                last_err = e
                # Retry only transient errors.
                if any(code in msg for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    if attempt < max_attempts - 1:
                        time.sleep(2 ** attempt)  # 1s, 2s
                        continue
                # Try the next model for other errors.
                break

    raise SummarizerError(
        f"Gemini request failed after trying {len(tried_models)} model(s): {last_err}"
    )


def summarize_video(
    title: str,
    transcript: str,
    description: Optional[str] = None,
) -> Tuple[str, str]:
    """Return the summary text and model name."""
    client = _get_client()

    if not transcript or not transcript.strip():
        if description:
            transcript = f"[No transcript. Summarize based on the description.]\n{description}"
        else:
            raise SummarizerError("There is no transcript or description to summarize.")

    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n...(truncated)"

    prompt = VIDEO_SUMMARY_PROMPT.format(
        title=title or "(No title)",
        transcript=transcript,
    )

    return _call_gemini_with_retry(client, prompt)


def summarize_trend(videos: List[dict]) -> Tuple[str, str]:
    client = _get_client()

    if not videos:
        raise SummarizerError("There are no videos to analyze.")

    # Limit to the latest N videos (assumes published_at is sorted descending).
    videos = videos[:TREND_MAX_VIDEOS]

    lines = []
    for v in videos:
        title = (v.get("title") or "").strip()
        channel = (v.get("channel_title") or "").strip()
        published = (v.get("published_at") or "")[:10]
        duration_min = int((v.get("duration_seconds") or 0) / 60)
        summary = (v.get("summary_text") or "").strip()
        excerpt = summary[:200] + ("..." if len(summary) > 200 else "") if summary else ""
        lines.append(
            f"- [{channel}] ({published}, {duration_min} min) {title}"
            + (f"\n  Summary: {excerpt}" if excerpt else "")
        )

    prompt = TREND_SUMMARY_PROMPT.format(video_list="\n".join(lines))

    return _call_gemini_with_retry(client, prompt)


def _extract_text(response) -> str:
    try:
        if response.text:
            return response.text
    except Exception:
        pass
    try:
        for c in (response.candidates or []):
            for p in (getattr(c.content, "parts", []) or []):
                t = getattr(p, "text", "")
                if t:
                    return t
    except Exception:
        pass
    return ""
