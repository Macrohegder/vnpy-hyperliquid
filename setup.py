from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="vnpy-hyperliquid",
    version="2026.04.20",
    author="QuantDev",
    author_email="",
    description="Hyperliquid gateway for VeighNa trading framework",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/raymond-hsiao/vnpy-hyperliquid",
    packages=find_packages(),
    package_data={"vnpy_hyperliquid": ["*.py", "py.typed"]},
    install_requires=[
        "vnpy>=3.9.0",
        "vnpy_rest>=1.2.0",
        "vnpy_websocket>=1.1.0",
        "hyperliquid-python-sdk>=0.10.0",
        "eth-account>=0.13.0",
        "msgpack>=1.0.0",
        "eth-utils>=5.0.0",
    ],
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial :: Investment",
        "Programming Language :: Python :: Implementation :: CPython",
        "License :: OSI Approved :: MIT License",
    ],
)
