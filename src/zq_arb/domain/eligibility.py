"""Deployment countries approved for this application; venue eligibility is separate."""

SUPPORTED_LIVE_COUNTRIES = frozenset({"HK", "NL"})
SUPPORTED_LIVE_COUNTRIES_LABEL = ", ".join(sorted(SUPPORTED_LIVE_COUNTRIES))


def polymarket_api_opening_permitted(*, checked: bool, country: str | None) -> bool:
    """Require a successful country check; the venue's blocked flag is informational."""
    return checked and country in SUPPORTED_LIVE_COUNTRIES
