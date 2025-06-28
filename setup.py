from setuptools import setup, find_packages

setup(
    name="mybot",
    version="0.1",
    packages=find_packages(),   # detecta PocketOptionAPI y pocketoptionapi_async
    install_requires=[
        "python-telegram-bot>=20.0",
        "requests",
        "pandas",
        "numpy",
        "ta"
    ],
    entry_points={
        "console_scripts": [
            "run-bot = bot:main",
        ]
    }
)
