"""
PrivaGlot core — the headless conversation kernel.

The kernel knows how a lesson runs (orchestration, greeting, vocabulary
statistics, end-of-lesson evaluation, continuity) and knows nothing about
any transport. Every user-facing surface — the WebSocket frontend, the
Feishu bot, future channels — is a thin adapter in app/channels/ that
translates platform messages into kernel calls and kernel events into
platform messages.

The architecture contract is spelled out in the docstrings of this
package and of app/channels/.
"""
