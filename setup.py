#!/usr/bin/env python
from setuptools import setup, find_packages

setup(
    name="tap-xero",
    version="2.0.4",
    description="Singer.io tap for extracting data from the Xero API",
    author="Stitch",
    url="http://singer.io",
    classifiers=["Programming Language :: Python :: 3 :: Only"],
    py_modules=["tap_xero"],
    install_requires=[
        "pipelinewise-singer-python==1.*",
        "requests==2.25.1",
    ],
    extras_require={"dev": ["ipdb", "pylint", "nose"]},
    entry_points="""
          [console_scripts]
          tap-xero=tap_xero:main
      """,
    packages=["tap_xero"],
    package_data={"schemas": ["tap_xero/schemas/*.json"]},
    include_package_data=True,
)
