import sys, types, importlib.util

# Stub job_status so jobs_check.py's module-level `import job_status` succeeds
# without needing the real deployment path.
job_status_stub = types.ModuleType('job_status')
job_status_stub.summarize = lambda: ([], True)
sys.modules['job_status'] = job_status_stub

spec = importlib.util.spec_from_file_location('jobs_check', 'jobs_check.py')
jobs_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jobs_check)

LIMIT = jobs_check.LIMIT
# One line far longer than LIMIT, with a marker placed past the truncation point.
long_line = ('X' * (LIMIT + 50)) + 'END_MARKER'

parts = jobs_check.compose([long_line], False, True)
combined = '\n'.join(parts)

assert 'END_MARKER' in combined, 'compose() dropped content past LIMIT instead of splitting it into another part'
