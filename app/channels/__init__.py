"""
PrivaGlot channels — transport adapters over the headless kernel.

Each channel translates platform messages into kernel calls and kernel
events (app/core/events.py) into platform messages. Channels never import
each other; the only thing they share is the kernel contract.

  web_ws.py   the original WebSocket frontend (full-fidelity)
  feishu/     the Feishu bot (voice-message, turn-based)
"""
