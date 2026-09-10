# Installer disk headroom preflight — 2026-09-10

The macOS installer now refuses before its first filesystem or service mutation
when the destination volume cannot safely retain the existing installation
during replacement. The estimate is derived from three source-sized transient
copies (build tree, wheel/archive, and pip unpack), the current runtime size,
and the total authority database size. A ten-percent margin with a 64 MiB
metadata/log floor covers filesystem overhead; an operator may replace that
margin with the explicit `DALTON_INSTALL_DISK_RESERVE_BYTES` value.

The check reads sizes and filesystem capacity only. It does not create the
Dalton root. Its refusal reports required and available bytes plus the measured
source, runtime, and database components so the threshold is reviewable rather
than an unexplained fixed gigabyte allowance.
