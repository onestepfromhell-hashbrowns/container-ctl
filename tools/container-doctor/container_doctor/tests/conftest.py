import os
import tempfile

# Tests are site-independent: point the probed package root at a temp directory that exists,
# before container_doctor.core.site_paths resolves it at import time.
os.environ.setdefault("CONTAINER_DOCTOR_OPT_ROOT", tempfile.mkdtemp(prefix="container-doctor-opt-"))
