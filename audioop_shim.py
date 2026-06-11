# Workaround for Python 3.14 missing audioop module
# This prevents discord.py from failing on import
import sys

if sys.version_info >= (3, 13):
    # Create a dummy audioop module for Python 3.14+
    import types
    audioop = types.ModuleType('audioop')
    sys.modules['audioop'] = audioop
