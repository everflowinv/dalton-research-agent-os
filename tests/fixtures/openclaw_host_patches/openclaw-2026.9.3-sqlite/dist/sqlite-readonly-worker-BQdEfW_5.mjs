import { t as hasErrnoCode } from "./errno-CkbDOfLk.mjs";
import { t as runtimeProcessEntrypoints } from "./runtime-process-entrypoints-DTToRHQ8.mjs";
import { n as resolveRuntimeWorkerUrl, t as resolveRuntimeWorkerArgv } from "./runtime-worker-url-BwKRa7FW.mjs";
import path from "node:path";
import { execFile, spawnSync } from "node:child_process";
//#region src/infra/sqlite-readonly-worker.ts
const SQLITE_READONLY_CHILD_ARG = "--openclaw-sqlite-readonly-child";
const SQLITE_INSPECTION_TIMEOUT_MS = 3e4;
function sqliteInspectionTimeoutError(operation, pathname) {
	return /* @__PURE__ */ new Error(`SQLite ${operation} timed out after 30 seconds for ${pathname}. Stop the Gateway service and other OpenClaw processes using this database, then retry; if already stopped, check storage performance.`);
}
function isSqliteReadOnlyWorkerResult(value) {
	if (!value || typeof value !== "object" || Array.isArray(value)) return false;
	if (Object.keys(value).length !== 2 || !("ok" in value)) return false;
	return value.ok === true && "location" in value && typeof value.location === "string" || value.ok === false && "message" in value && typeof value.message === "string";
}
function createSqliteReadOnlyWorkerError(message, stderr) {
	const stderrTail = stderr.trim().slice(-4e3);
	return /* @__PURE__ */ new Error(`SQLite read-only worker ${message}${stderrTail ? `\nstderr (tail): ${stderrTail}` : ""}`);
}
function parseSqliteReadOnlyWorkerResult(stdout, stderr) {
	if (!stdout.trim()) throw createSqliteReadOnlyWorkerError("returned no JSON result", stderr);
	let message;
	try {
		message = JSON.parse(stdout);
	} catch {
		throw createSqliteReadOnlyWorkerError("returned invalid JSON", stderr);
	}
	if (!isSqliteReadOnlyWorkerResult(message)) throw createSqliteReadOnlyWorkerError("returned an invalid result", stderr);
	return message;
}
function readSqliteReadOnlyWorkerLocation(params) {
	let result;
	try {
		result = parseSqliteReadOnlyWorkerResult(params.stdout, params.stderr);
	} catch (error) {
		if (params.failure) throw createSqliteReadOnlyWorkerError(params.failure, params.stderr);
		throw error;
	}
	if (params.failure || !result.ok) throw createSqliteReadOnlyWorkerError(!result.ok ? result.message : params.failure ?? "failed", params.stderr);
	return result.location;
}
function sqliteReadOnlyWorkerArgv(pathname, mode, stagingRoot) {
	const workerUrl = resolveRuntimeWorkerUrl(runtimeProcessEntrypoints.sqliteReadOnly);
	return [
		...resolveRuntimeWorkerArgv(workerUrl),
		SQLITE_READONLY_CHILD_ARG,
		mode,
		path.resolve(pathname),
		...stagingRoot ? [stagingRoot] : []
	];
}
function runSqliteReadOnlyWorker(pathname, options) {
	return new Promise((resolve, reject) => {
		let output = {
			stderr: "",
			stdout: ""
		};
		const child = execFile(process.execPath, sqliteReadOnlyWorkerArgv(pathname, options.mode, options.stagingRoot), {
			encoding: "utf8",
			timeout: SQLITE_INSPECTION_TIMEOUT_MS,
			killSignal: "SIGKILL"
		}, (error, stdout, stderr) => {
			output = {
				failure: error ? error.killed && error.signal === "SIGKILL" && error.code == null ? sqliteInspectionTimeoutError("read-only snapshot", pathname).message : `exited unsuccessfully: ${error.message}` : void 0,
				stderr,
				stdout
			};
		});
		const abort = () => {
			child.kill("SIGKILL");
		};
		options.signal?.addEventListener("abort", abort, { once: true });
		if (options.signal?.aborted) abort();
		child.once("close", () => {
			options.signal?.removeEventListener("abort", abort);
			try {
				options.signal?.throwIfAborted();
				resolve(readSqliteReadOnlyWorkerLocation(output));
			} catch (workerError) {
				reject(workerError instanceof Error ? workerError : new Error(String(workerError)));
			}
		});
	});
}
function runSqliteReadOnlyWorkerSync(pathname, stagingRoot) {
	const result = spawnSync(process.execPath, sqliteReadOnlyWorkerArgv(pathname, "sync", stagingRoot), {
		encoding: "utf8",
		timeout: SQLITE_INSPECTION_TIMEOUT_MS,
		killSignal: "SIGKILL"
	});
	return readSqliteReadOnlyWorkerLocation({
		failure: result.error ? hasErrnoCode(result.error, "ETIMEDOUT") ? sqliteInspectionTimeoutError("read-only snapshot", pathname).message : `failed to start: ${result.error.message}` : result.status === 0 ? void 0 : `exited with ${result.signal ? `signal ${result.signal}` : `code ${result.status}`}`,
		stderr: result.stderr,
		stdout: result.stdout
	});
}
//#endregion
export { sqliteInspectionTimeoutError as a, runSqliteReadOnlyWorkerSync as i, SQLITE_READONLY_CHILD_ARG as n, runSqliteReadOnlyWorker as r, SQLITE_INSPECTION_TIMEOUT_MS as t };
