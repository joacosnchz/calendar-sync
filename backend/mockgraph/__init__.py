"""A local stand-in for Microsoft identity platform + Microsoft Graph.

Implements exactly the surface Calendar Sync uses (delegated auth code sign-in with
incremental consent, refresh tokens with rotation and revocation, reading events,
calendarView delta, subscriptions with validation handshake and change/lifecycle
notifications) for source tenants, plus a control panel to drive scenarios by hand.
Never use it in production.
"""
