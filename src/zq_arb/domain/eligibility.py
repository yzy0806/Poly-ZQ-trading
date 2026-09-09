"""Deployment countries approved for this application; venue eligibility is separate."""

SUPPORTED_LIVE_COUNTRIES = frozenset({"HK", "NL"})
SUPPORTED_LIVE_COUNTRIES_LABEL = ", ".join(sorted(SUPPORTED_LIVE_COUNTRIES))
