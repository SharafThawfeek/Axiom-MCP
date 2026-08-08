"""The target library list — which repos the offline indexer crawls.

Scoped to popular, actively-maintained Python libraries where users commonly
hit version-specific breakage: data/ML, web frameworks, and HTTP clients.
Extend this list to widen the index; each entry costs one GitHub crawl.
"""

# (pypi_name, "owner/repo")
TARGET_LIBRARIES: list[tuple[str, str]] = [
    ("requests", "psf/requests"),
    ("pandas", "pandas-dev/pandas"),
    ("numpy", "numpy/numpy"),
    ("flask", "pallets/flask"),
    ("fastapi", "fastapi/fastapi"),
    ("django", "django/django"),
    ("sqlalchemy", "sqlalchemy/sqlalchemy"),
    ("pydantic", "pydantic/pydantic"),
    ("pytest", "pytest-dev/pytest"),
    ("celery", "celery/celery"),
    ("scikit-learn", "scikit-learn/scikit-learn"),
    ("matplotlib", "matplotlib/matplotlib"),
    ("pillow", "python-pillow/Pillow"),
    ("click", "pallets/click"),
    ("httpx", "encode/httpx"),
    ("aiohttp", "aio-libs/aiohttp"),
    ("boto3", "boto/boto3"),
    ("pyyaml", "yaml/pyyaml"),
    ("jinja2", "pallets/jinja"),
    ("cryptography", "pyca/cryptography"),
]
