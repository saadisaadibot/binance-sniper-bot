# wsgi.py
import sys, traceback

try:
    from main import app  # main = اسم الملف main.py
except Exception as e:
    print("WSGI_IMPORT_ERROR:", repr(e), file=sys.stderr)
    traceback.print_exc()
    raise