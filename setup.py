from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="discoolpy",
    version="0.1.0b1",
    description="A modular, python-based district-cooling modelling tool built on TESPy.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Anas Algarei",
    url="https://github.com/AnasAlgarei/discoolpy",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering",
    ],
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=[
        "tespy>=0.9.16",
        "CoolProp>=6.4",
        "numpy>=1.21",
        "pandas>=1.5",
        "matplotlib>=3.5",
        "pyyaml>=6.0",
        "scipy>=1.7",
    ],
    extras_require={
        "notebooks": [
            "jupyter>=1.0",
            "nbconvert>=7.0",
        ],
        "dev": [
            "pytest>=7.0",
            "black",
            "flake8",
        ],
    },
)
