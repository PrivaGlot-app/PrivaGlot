"""
Native-language panel translation (#52) — the teacher's reply rendered in
the student's mother tongue inside the 「听不懂」collapse panel.

A deliberate second, independent LLM call (the same shape as the two guard
retries, minus the rewrite): the teaching call stays lean, the translation
prompt is versioned on its own, and the three isolation rules hold by
construction — the translation never enters the conversation history, never
goes to TTS, never passes the language guard. Fail-open by contract: any
failure or timeout simply leaves the panel original-only.
"""
from .. import config


async def translate_reply(llm, text: str, native_language: str) -> str:
    """Translate the final (post-guard) reply text into the student's
    native language. `llm` is the shared LLM engine port — same vendor and
    model as the class, but a short call: no history, short prompt, output
    capped by the same per-turn budget. Returns "" when nothing usable
    comes back (the kernel then yields no translation event)."""
    if not (text or "").strip() or not (native_language or "").strip():
        return ""
    system_prompt = config.TRANSLATION_SYSTEM_PROMPT.format(
        native_language=native_language)
    out = (await llm.generate(
        text, history=[], system_prompt=system_prompt,
        max_tokens=config.LLM_MAX_TOKENS_PER_TURN) or "").strip()
    return _strip_wrapping(out)


def _strip_wrapping(out: str) -> str:
    """Best-effort cleanup of the two disobedience shapes the prompt already
    forbids but models still occasionally produce (a wrapping pair of quotes
    or a leading 「翻译：」-style label)."""
    out = (out or "").strip()
    if len(out) >= 2 and out[0] in "\"“”「『" and out[-1] in "\"“”「』":
        out = out[1:-1].strip()
    for label in ("翻译：", "翻译:", "译文：", "译文:", "Translation:"):
        if out.startswith(label):
            out = out[len(label):].strip()
    return out
