"""Aggregato — a single-user, self-hosted media log aggregator."""

from importlib.metadata import version

__version__ = version("aggregato")

#: The provider plugin API version this host implements (contracts/provider-plugin.md).
PROVIDER_API_VERSION = (1, 0)
