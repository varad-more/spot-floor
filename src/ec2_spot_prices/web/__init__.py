"""Web layer: a read-only dashboard over the time-series store."""

from ec2_spot_prices.web.app import WebConfig, build_providers, create_app

__all__ = ["WebConfig", "build_providers", "create_app"]
