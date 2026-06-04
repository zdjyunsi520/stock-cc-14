## [ERR-20260604-001] apply_patch_large_file_quote_conflict

**Logged**: 2026-06-04T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Large `apply_patch` payload failed because shell quoting conflicted with embedded single quotes.

### Error
```text
/usr/bin/bash: -c: line 141: unexpected EOF while looking for matching `'`
```

### Context
- Command attempted: `apply_patch <<'PATCH'` with a large new Python file.
- The payload contained many embedded quotes and exceeded the comfortable size for manual shell patching.

### Suggested Fix
For large newly-created files, use the dedicated Write tool; keep `apply_patch` for smaller edits to existing files.

### Metadata
- Reproducible: yes
- Related Files: src/services/evolution_service.py

---

## [ERR-20260604-002] apply_patch_command_missing

**Logged**: 2026-06-04T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
`apply_patch` is not available in this bash environment.

### Error
```text
/usr/bin/bash: line 41: apply_patch: command not found
```

### Context
- Command attempted: `apply_patch <<'PATCH'` for a small config edit.
- Environment: Windows workspace through bash.

### Suggested Fix
Use the dedicated Edit tool for existing files and Write for new files when `apply_patch` is unavailable.

### Metadata
- Reproducible: yes
- Related Files: src/config.py

---

## [ERR-20260604-003] ci_gate_python3_missing

**Logged**: 2026-06-04T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
The repository test script assumes `python3`, but this environment only exposes `python`.

### Error
```text
[ERROR] Python3 未安装
/usr/bin/bash: line 1: python3: command not found
```

### Context
- Command attempted: `./scripts/ci_gate.sh`
- `python --version` succeeded, `python3 --version` failed.
- The failure occurred in `scripts/test.sh` before deterministic checks could run.

### Suggested Fix
Make the test script resolve a Python interpreter dynamically, falling back from `python3` to `python`.

### Metadata
- Reproducible: yes
- Related Files: scripts/test.sh, scripts/ci_gate.sh

---
