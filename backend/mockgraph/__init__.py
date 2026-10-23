"""A local stand-in for Microsoft identity platform + Microsoft Graph.

Implements exactly the surface Calendar Sync uses (client credentials, auth code sign-in,
admin consent, events, calendarView delta, subscriptions with validation handshake and
change/lifecycle notifications) plus a control panel to drive scenarios by hand.
Never use it in production.
"""
