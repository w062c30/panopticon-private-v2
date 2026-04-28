import sys; sys.path.insert(0, 'd:/Antigravity/Panopticon')
from panopticon_py.db import ShadowDB
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
db = ShadowDB(tmp); db.bootstrap()
result = db.resolve_slug('missing_token')
print(f'resolve_slug("missing_token") = "{result}"')
db.close(); os.unlink(tmp)