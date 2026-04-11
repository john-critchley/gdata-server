#!/usr/bin/env python3
"""
Notes Browser - Hypertext browser for gdata notes system
"""

from setuptools import setup, find_packages

setup(
    name='notes-browser',
    version='0.1.0',
    description='Hypertext browser for gdata notes system',
    author='Note System',
    py_modules=['notes_browser'],
    entry_points={
        'console_scripts': [
            'notes-browser=notes_browser:main',
        ],
    },
    install_requires=[
        'wxPython>=4.1.0',
        'requests',
    ],
    python_requires='>=3.6',
)
