import os

# Force mock providers + auth-off for the whole test suite, before config is imported.
os.environ.setdefault("MAESTRO_ALLOW_MOCK", "true")
os.environ.setdefault("MAESTRO_ENV", "test")
os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("MAESTRO_API_KEYS", None)
