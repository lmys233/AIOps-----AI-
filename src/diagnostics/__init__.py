"""Language neutral diagnostic contracts; no application or agent imports."""
from .contract import SCHEMA_VERSION, event, validate_event

__all__ = ['SCHEMA_VERSION', 'event', 'validate_event']
