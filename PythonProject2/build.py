import PyInstaller.__main__
import os

PyInstaller.__main__.run([
    'botds.py',
    '--onefile',
    '--add-data', '.env;.',
    '--add-data', 'long_term_memory.json;.',
    '--add-data', 'short_term_memory.json;.',
    '--name', 'NuttyBoarBot',
    '--icon', 'NONE',
    '--clean'
])