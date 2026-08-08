"""The target library list — which repos the offline indexer crawls.

Scoped to popular, actively-maintained libraries where users commonly hit
version-specific breakage. Extend this list to widen the index; each entry
costs one GitHub crawl.

The language is stored on every incident so retrieval can filter by it. A
Python TypeError and a JavaScript TypeError describe genuinely different
failures, and without the filter a mixed index will happily return one for
the other — they're close enough in wording to clear the similarity
threshold.
"""

# (package_name, "owner/repo", language)
TARGET_LIBRARIES: list[tuple[str, str, str]] = [
    # ---- Python ----
    ("requests", "psf/requests", "python"),
    ("pandas", "pandas-dev/pandas", "python"),
    ("numpy", "numpy/numpy", "python"),
    ("flask", "pallets/flask", "python"),
    ("fastapi", "fastapi/fastapi", "python"),
    ("django", "django/django", "python"),
    ("sqlalchemy", "sqlalchemy/sqlalchemy", "python"),
    ("pydantic", "pydantic/pydantic", "python"),
    ("pytest", "pytest-dev/pytest", "python"),
    ("celery", "celery/celery", "python"),
    ("scikit-learn", "scikit-learn/scikit-learn", "python"),
    ("matplotlib", "matplotlib/matplotlib", "python"),
    ("pillow", "python-pillow/Pillow", "python"),
    ("click", "pallets/click", "python"),
    ("httpx", "encode/httpx", "python"),
    ("aiohttp", "aio-libs/aiohttp", "python"),
    ("boto3", "boto/boto3", "python"),
    ("pyyaml", "yaml/pyyaml", "python"),
    ("jinja2", "pallets/jinja", "python"),
    ("cryptography", "pyca/cryptography", "python"),

    # ---- JavaScript / TypeScript ----
    ("react", "facebook/react", "javascript"),
    ("next", "vercel/next.js", "javascript"),
    ("express", "expressjs/express", "javascript"),
    ("typescript", "microsoft/TypeScript", "javascript"),
    ("vite", "vitejs/vite", "javascript"),
    ("webpack", "webpack/webpack", "javascript"),
    ("jest", "jestjs/jest", "javascript"),
    ("axios", "axios/axios", "javascript"),
    ("vue", "vuejs/core", "javascript"),
    ("svelte", "sveltejs/svelte", "javascript"),
    ("eslint", "eslint/eslint", "javascript"),
    ("prisma", "prisma/prisma", "javascript"),
    ("mongoose", "Automattic/mongoose", "javascript"),
    ("tailwindcss", "tailwindlabs/tailwindcss", "javascript"),
    ("esbuild", "evanw/esbuild", "javascript"),
]
