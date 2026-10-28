"""
Prompt Logger - Records complete LLM messages arrays for debugging.

Enabled via DEBUG_PROMPTS=1 environment variable.
When disabled, all methods are no-ops with zero overhead.

Output: logs/prompt_logs/{session_id}_prompts.jsonl
Each line is one JSON object containing the full messages array sent to the LLM
and the complete response received.
"""
import json
from datetime import datetime
from . import config
from .logging_config import log as logger


class PromptLogger:
    def __init__(self):
        self.enabled = config.DEBUG_PROMPTS
        if self.enabled:
            config.PROMPT_LOG_DIR.mkdir(exist_ok=True)
            logger.info(f"Prompt logging ENABLED → {config.PROMPT_LOG_DIR}")

    def log_turn(self, session_id, turn_number, system_prompt, history,
                 user_input, response, metadata=None):
        """Log one complete LLM call: messages array + response.

        Args:
            session_id:    Session identifier (timestamp-based)
            turn_number:   Current turn number (1-based)
            system_prompt: The full system prompt string
            history:       conversation_history list (role/content dicts)
            user_input:    The student's current transcribed text
            response:      The LLM's full response text
            metadata:      Optional dict with extra context (student, lesson, etc.)
        """
        if not self.enabled:
            return

        # Reconstruct the exact messages array sent to the LLM
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_input})

        entry = {
            # Same format as dialogue_logger (ISO, seconds) so the two logs align
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "turn": turn_number,
            "messages": messages,
            "response": response,
            "metadata": metadata or {},
        }

        filepath = config.PROMPT_LOG_DIR / f"{session_id}_prompts.jsonl"
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"PromptLogger failed: {e}")


prompt_logger = PromptLogger()
