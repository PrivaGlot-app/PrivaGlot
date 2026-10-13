"""
Feishu channel package.

  client.py   Open Platform REST calls (token / download / upload / send),
              plain httpx; the SDK only handles the WebSocket long
              connection for event subscription.
  channel.py  the channel itself: event dispatch, command parsing,
              identity binding, kernel event → Feishu message rendering.
"""
