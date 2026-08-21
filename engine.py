#!/usr/bin/env python3
"""Point d'entrée CLI: `python engine.py <commande>` (voir app/cli/main.py)."""
import sys

from app.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
