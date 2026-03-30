#!/usr/libexec/platform-python -tt
"""InstanceHA entry point - thin wrapper that imports and runs the package."""

import sys
import os

# Add the script directory to Python path so the instanceha package can be found
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from instanceha.main import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        import logging
        logging.info("Shutting down due to keyboard interrupt")
    except Exception as e:
        import logging
        logging.error(f'Error: {e}')
        raise
