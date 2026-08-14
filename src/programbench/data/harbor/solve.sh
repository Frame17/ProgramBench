#!/bin/bash
# ProgramBench tasks are black-box by design: the reference source is removed
# from the environment, so no runnable gold solution is shipped. The verifier
# scores whatever /workspace/compile.sh produces at /workspace/executable.
echo "No reference solution ships with ProgramBench reverse-engineering tasks." >&2
exit 1
