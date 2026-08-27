"""Slowapi rate limiter setup — single shared instance for the whole app."""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Key function: rate-limit by client IP
limiter = Limiter(key_func=get_remote_address)
