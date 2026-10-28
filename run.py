"""Entry point (also the PyInstaller script): python run.py [--debug]"""
import sys

from skieo_creator.main import main

if __name__ == '__main__':
    sys.exit(main())
