// Excerpt of OpenClaw 2026.9.6 dist/sqlite-readonly-worker-CmkAsqCm.mjs (budget region only).
//#region src/infra/sqlite-readonly-worker.ts
const SQLITE_INSPECTION_TIMEOUT_MS = 3e5;
const SQLITE_INSPECTION_BYTES_PER_SECOND = 33554432;
const log = createSubsystemLogger("state/sqlite");
function resolveSqliteInspectionBudget(operation, pathname, sizeBytes) {
	const timeoutMs = resolveTimerTimeoutMs(SQLITE_INSPECTION_TIMEOUT_MS + Math.ceil(40 * Number(sizeBytes ?? 0) / SQLITE_INSPECTION_BYTES_PER_SECOND) * 1e3, SQLITE_INSPECTION_TIMEOUT_MS);
	const size = sizeBytes === void 0 ? "unknown size" : formatByteSize(Number(sizeBytes), {
		style: "iec",
		maxUnit: "giga",
		separator: " ",
		fractionDigits: sizeBytes < 1024n ? 0 : 1
	});
	if (timeoutMs > SQLITE_INSPECTION_TIMEOUT_MS) log.debug(`SQLite ${operation} for ${pathname}: ${size}, budget ${timeoutMs / 1e3} seconds`);
	return {
		timeoutMs,
		size
	};
}
/** Sum serial SQLite inspection budgets without overflowing Node timers. */
function resolveAggregateSqliteInspectionTimeoutMs(operation, databases) {
	let timeoutMs = 0;
	for (const database of databases) timeoutMs += resolveSqliteInspectionBudget(operation, database.path, database.sizeBytes).timeoutMs;
	return resolveTimerTimeoutMs(timeoutMs, SQLITE_INSPECTION_TIMEOUT_MS, SQLITE_INSPECTION_TIMEOUT_MS);
}
function readSqliteInspectionBudget(operation, pathname, mainSizeBytes) {
	let sizeBytes = mainSizeBytes;
	try {
		sizeBytes ??= fs.statSync(pathname, { bigint: true }).size;
		for (const suffix of [
			"-wal",
			"-shm",
			"-journal"
		]) try {
			sizeBytes += fs.statSync(pathname + suffix, { bigint: true }).size;
		} catch (error) {
			if (!hasErrnoCode(error, "ENOENT")) throw error;
		}
	} catch {}
	return resolveSqliteInspectionBudget(operation, pathname, sizeBytes);
}
function sqliteInspectionTimeoutError(operation, pathname, timeoutMs, size) {
	return /* @__PURE__ */ new Error(`SQLite ${operation} timed out after ${timeoutMs / 1e3} seconds (budget for ${size}) for ${pathname}. Stop the Gateway service and other OpenClaw processes using this database, then retry; if already stopped, check storage performance.`);
}
//#endregion
export { readSqliteInspectionBudget as a, resolveAggregateSqliteInspectionTimeoutMs as o, resolveSqliteInspectionBudget as s, sqliteInspectionTimeoutError as d };
