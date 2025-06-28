from setuptools import setup, find_packages

setup(
    name="pocketoptionapi_async",
    version="0.1.0",
    packages=find_packages(
        include=["pocketoptionapi_async", "pocketoptionapi_async.*"]
    ),
    install_requires=[
        r.strip() for r in open("requirements.txt") if r.strip() and not r.startswith("#")
    ],
    # si hubiera extras, los puedes añadir aquí...
)
