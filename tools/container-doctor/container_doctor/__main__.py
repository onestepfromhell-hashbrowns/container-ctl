"""
Container Doctor module entrypoint (python3 -m container_doctor).
"""

import sys
from container_doctor.cli import main

if __name__ == "__main__":
    sys.exit(main())
