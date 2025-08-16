import sys, traceback
try:
    from main import app
except Exception as e:
    print("WSGI_IMPORT_ERROR:", repr(e), file=sys.stderr)
    traceback.print_exc()
    raise