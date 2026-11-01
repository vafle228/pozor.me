"""Pre-commit rejects files matched by the private-runtime-files hook."""
import sys

if len(sys.argv) > 1:
    print('Refusing private configuration or runtime data. Keep it outside Git.')
    raise SystemExit(1)
