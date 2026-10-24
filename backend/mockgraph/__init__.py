"""A local stand-in for Microsoft identity platform + Microsoft Graph.

Implements exactly the surface Calendar Sync uses (delegated auth code sign-in with
incremental consent, refresh tokens with rotation and revocation, client credentials for
the primary app, events, calendarView delta, subscriptions with validation handshake and
change/lifecycle notifications) plus a control panel to drive scenarios by hand.
Never use it in production.
"""
