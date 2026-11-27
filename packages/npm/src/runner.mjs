import { join } from "node:path";
import { createInterface } from "node:readline";

import { BOLD, CYAN, DIM, GREEN, RED, YELLOW, assignServiceColors, style, supportsColor } from "./colors.mjs";
import { loadEnvFiles } from "./dotenv.mjs";
import { isFile } from "./fs_utils.mjs";
import { createTaskResult } from "./models.mjs";
import { checkTaskPorts } from "./ports.mjs";
import {
  scheduleDescendantSnapshot,
  spawnManagedProcess,
  terminateManagedProcess,
} from "./proc_tree.mjs";
import { probeHttp, probeTcp } from "./readiness.mjs";
import { diffSnapshots, snapshotTree } from "./watcher.mjs";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatTimestamp() {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");
  const ms = String(now.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

class OutputWriter {
  constructor(stream, serviceColors, padWidth, useColor, timestamps, showPrefix) {
    this._stream = stream;
    this._colors = serviceColors;
    this._padWidth = padWidth;
    this._useColor = useColor;
    this._timestamps = timestamps;
    this._showPrefix = showPrefix;
  }

  writeLine(service, line) {
    const parts = [];
    if (this._timestamps) {
      parts.push(style(formatTimestamp(), DIM, { enabled: this._useColor }));
    }
    if (this._showPrefix) {
      const tag = `[${service.padEnd(this._padWidth, " ")}]`;
      const color = this._colors[service] || "";
      parts.push(style(tag, BOLD, color, { enabled: this._useColor && Boolean(color) }));
    }
    const prefix = parts.join(" ");
    const formatted = prefix ? `${prefix} ${line}` : line;
    this._stream.write(`${formatted}\n`);
  }

  writeSystem(message, colorCode = CYAN) {
    const tsPrefix = this._timestamps
      ? `${style(formatTimestamp(), DIM, { enabled: this._useColor })} `
      : "";
    const tag = style("[devup]", BOLD, colorCode, { enabled: this._useColor });
    this._stream.write(`${tsPrefix}${tag} ${message}\n`);
  }
}

export function expandDependencies(config, initialTargets) {
  const ordered = [];
  const state = new Map(); // 0=unvisited, 1=visiting, 2=done

  function visit(name, trail) {
    const current = state.get(name) || 0;
    if (current === 2) return;
    if (current === 1) {
      const cycle = [...trail, name].join(" -> ");
      throw new Error(`Circular task dependency detected: ${cycle}`);
    }
    if (!Object.hasOwn(config.tasks, name)) {
      const err = new Error(`Task '${name}' referenced in depends_on was not found.`);
      err.code = "ERR_UNKNOWN_DEPENDENCY";
      throw err;
    }
    state.set(name, 1);
    for (const dep of config.tasks[name].dependsOn) {
      visit(dep, [...trail, name]);
    }
    state.set(name, 2);
    ordered.push(name);
  }

  for (const target of initialTargets) {
    visit(target, []);
  }
  return ordered;
}

function loadWorkspaceDotenv(config, opts) {
  const merged = {};
  const warnings = [];

  if (opts.dotenv && config.autoEnv) {
    const defaultEnv = join(config.root, ".env");
    if (isFile(defaultEnv)) {
      const loaded = loadEnvFiles([defaultEnv], config.root, false);
      Object.assign(merged, loaded.env);
      warnings.push(...loaded.warnings);
    }
  }

  const explicitFiles = [...config.envFiles, ...opts.extraEnvFiles];
  if (explicitFiles.length > 0) {
    const loaded = loadEnvFiles(explicitFiles, config.root, true);
    Object.assign(merged, loaded.env);
    warnings.push(...loaded.warnings);
  }

  return { env: merged, warnings };
}

function buildTaskEnv(config, task, workspaceDotenv) {
  const env = { ...process.env };
  if (!Object.hasOwn(env, "PYTHONUNBUFFERED")) {
    env.PYTHONUNBUFFERED = "1";
  }
  for (const [k, v] of Object.entries(workspaceDotenv)) {
    if (!Object.hasOwn(env, k)) {
      env[k] = v;
    }
  }
  Object.assign(env, config.env);

  const warnings = [];
  if (task.envFiles && task.envFiles.length > 0) {
    const loaded = loadEnvFiles(task.envFiles, task.cwd || config.root, true);
    Object.assign(env, loaded.env);
    warnings.push(...loaded.warnings);
  }
  Object.assign(env, task.env);
  return { env, warnings };
}

function createDeferred() {
  let resolved = false;
  let resolveFn;
  const promise = new Promise((res) => {
    resolveFn = () => {
      if (!resolved) {
        resolved = true;
        res();
      }
    };
  });
  return {
    promise,
    isSet: () => resolved,
    set: () => resolveFn(),
  };
}

function printSummary(writer, results, useColor, interrupted) {
  if (results.length === 0) return;
  const parts = results.map((res) => {
    let badge;
    if (res.status === "ok") {
      badge = style("OK", GREEN, { enabled: useColor });
    } else if (res.status === "stopped") {
      badge = style("STOPPED", YELLOW, { enabled: useColor });
    } else if (res.status === "skipped") {
      badge = style("SKIPPED", YELLOW, { enabled: useColor });
    } else {
      badge = style(`FAIL(${res.exitCode})`, RED, { enabled: useColor });
    }
    return `${res.name}:${badge}`;
  });

  const header = interrupted ? "Interrupted" : "Summary";
  writer.writeSystem(`${header}: ${parts.join(" | ")}`, CYAN);
}

export async function runTasks(config, targets, options = {}, stream = process.stdout, abortSignal = null) {
  const opts = {
    failFast: null,
    shutdownTimeout: null,
    timestamps: false,
    prefix: true,
    color: null,
    dryRun: false,
    dotenv: true,
    extraEnvFiles: [],
    portCheck: true,
    strictPorts: null,
    watch: false,
    watchInterval: 0.4,
    ...options,
  };

  const out = stream || process.stdout;
  const useColor = supportsColor(out, opts.color);
  const failFast = opts.failFast === null ? config.failFast : opts.failFast;
  const strictPorts = opts.strictPorts === null ? config.strictPorts : opts.strictPorts;
  const shutdownTimeout =
    opts.shutdownTimeout === null ? config.shutdownTimeout : opts.shutdownTimeout;

  const orderedNames = expandDependencies(config, targets);
  if (orderedNames.length === 0) return [];

  const padWidth = Math.min(24, Math.max(...orderedNames.map((n) => n.length)));
  const colors = assignServiceColors(orderedNames, useColor);
  const writer = new OutputWriter(out, colors, padWidth, useColor, opts.timestamps, opts.prefix);

  if (opts.dryRun) {
    writer.writeSystem(`Dry run execution plan (${orderedNames.length} task(s)):`, CYAN);
    const results = [];
    for (const name of orderedNames) {
      const task = config.tasks[name];
      const depsNote =
        task.dependsOn.length > 0 ? ` (after: ${task.dependsOn.join(", ")})` : "";
      const cwdNote = ` @ ${task.cwd || config.root}`;
      writer.writeLine(name, `${task.command}${depsNote}${cwdNote}`);
      results.push(createTaskResult({ name, exitCode: 0, durationSeconds: 0.0, status: "ok" }));
    }
    return results;
  }

  const workspaceEnvLoaded = loadWorkspaceDotenv(config, opts);
  const workspaceDotenv = workspaceEnvLoaded.env;
  for (const warn of workspaceEnvLoaded.warnings) {
    writer.writeSystem(warn, YELLOW);
  }

  if (opts.portCheck) {
    const conflicts = await checkTaskPorts(config.tasks, orderedNames);
    if (conflicts.length > 0) {
      for (const conflict of conflicts) {
        writer.writeSystem(
          `Port ${strictPorts ? "error" : "warning"} (${conflict.taskName}): ${conflict.reason}`,
          strictPorts ? RED : YELLOW
        );
      }
      if (strictPorts) {
        const conflictByTask = new Map(conflicts.map((c) => [c.taskName, c]));
        return orderedNames.map((name) => {
          const c = conflictByTask.get(name);
          return createTaskResult({
            name,
            exitCode: c ? 2 : 1,
            durationSeconds: 0.0,
            status: c ? "error" : "skipped",
            error: c ? c.reason : null,
          });
        });
      }
    }
  }

  const readyGates = Object.fromEntries(orderedNames.map((n) => [n, createDeferred()]));
  const depFailed = Object.fromEntries(orderedNames.map((n) => [n, false]));
  const readyRegexes = Object.fromEntries(
    orderedNames.map((n) => [
      n,
      config.tasks[n].readyPattern ? new RegExp(config.tasks[n].readyPattern) : null,
    ])
  );

  let stopRequested = Boolean(abortSignal?.aborted);
  let forceKillRequested = false;
  let interrupted = false;
  const activeProcs = new Map();
  const resultsMap = new Map();
  const stopPromises = [];

  async function stopAll(reason, force = false) {
    stopRequested = true;
    if (force) forceKillRequested = true;
    const snapshot = [...activeProcs.values()];
    if (snapshot.length > 0 && reason) {
      writer.writeSystem(reason, YELLOW);
    }
    await Promise.all(
      snapshot.map((managed) => terminateManagedProcess(managed, shutdownTimeout, force))
    );
    for (const gate of Object.values(readyGates)) {
      gate.set();
    }
  }

  const onAbort = () => {
    const p = stopAll("Stopped — shutting down running services...", false);
    stopPromises.push(p);
  };
  if (abortSignal) {
    abortSignal.addEventListener("abort", onAbort, { once: true });
  }

  const handleSignal = () => {
    if (!interrupted) {
      interrupted = true;
      stopRequested = true;
      stopPromises.push(stopAll("Interrupt received — stopping running services...", false));
    } else {
      forceKillRequested = true;
      stopPromises.push(stopAll("Second interrupt — force killing remaining services...", true));
    }
  };

  process.on("SIGINT", handleSignal);
  process.on("SIGTERM", handleSignal);

  writer.writeSystem(`Starting ${orderedNames.length} task(s): ${orderedNames.join(", ")}`, CYAN);

  async function runOne(name) {
    const task = config.tasks[name];

    for (const dep of task.dependsOn) {
      while (!readyGates[dep].isSet() && !stopRequested) {
        await Promise.race([readyGates[dep].promise, sleep(100)]);
      }
      if (stopRequested || depFailed[dep]) {
        depFailed[name] = true;
        readyGates[name].set();
        resultsMap.set(
          name,
          createTaskResult({
            name,
            exitCode: 1,
            durationSeconds: 0.0,
            status: "skipped",
            error: `Skipped because dependency '${dep}' did not become ready.`,
          })
        );
        writer.writeLine(
          name,
          style(`skipped (dependency '${dep}' failed or stopped)`, YELLOW, { enabled: useColor })
        );
        return;
      }
    }

    if (stopRequested) {
      depFailed[name] = true;
      readyGates[name].set();
      resultsMap.set(name, createTaskResult({ name, exitCode: 0, durationSeconds: 0.0, status: "skipped" }));
      return;
    }

    const isWatched = opts.watch || task.watchEnabled;
    const watchDir = task.cwd || config.root;
    const watchPatterns = task.watch && task.watch.length > 0 ? task.watch : null;
    let snap = isWatched ? snapshotTree(watchDir, watchPatterns) : new Map();

    const builtEnv = buildTaskEnv(config, task, workspaceDotenv);
    let env = builtEnv.env;
    for (const warn of builtEnv.warnings) {
      writer.writeLine(name, style(warn, YELLOW, { enabled: useColor }));
    }

    while (true) {
      const startMs = Date.now();
      if (stopRequested) {
        depFailed[name] = true;
        readyGates[name].set();
        resultsMap.set(name, createTaskResult({ name, exitCode: 0, durationSeconds: 0.0, status: "skipped" }));
        return;
      }

      let managed;
      try {
        managed = spawnManagedProcess(task.command, task.cwd || config.root, env);
      } catch (err) {
        depFailed[name] = true;
        readyGates[name].set();
        resultsMap.set(
          name,
          createTaskResult({
            name,
            exitCode: 127,
            durationSeconds: (Date.now() - startMs) / 1000,
            status: "error",
            error: err.message,
          })
        );
        writer.writeLine(name, style(`failed to start: ${err.message}`, RED, { enabled: useColor }));
        if (failFast && !isWatched) {
          await stopAll(`Task '${name}' failed to start — stopping other services...`, false);
        }
        return;
      }

      activeProcs.set(name, managed);

      const pattern = readyRegexes[name];
      const hasNetworkProbe = Boolean(task.readyUrl || task.readyTcp);
      let patternOk = pattern === null;
      let probeOk = !hasNetworkProbe;
      let attemptDone = false;
      let probeTimedOut = false;

      const maybeMarkReady = () => {
        if (patternOk && probeOk && !readyGates[name].isSet()) {
          readyGates[name].set();
        }
      };

      const handleOutputStream = (readable) => {
        if (!readable) return Promise.resolve();
        return new Promise((resolve) => {
          const rl = createInterface({ input: readable, crlfDelay: Infinity });
          rl.on("line", (line) => {
            writer.writeLine(name, line);
            if (pattern !== null && !patternOk && pattern.test(line)) {
              patternOk = true;
              maybeMarkReady();
            }
          });
          rl.once("close", resolve);
          rl.once("error", resolve);
        });
      };

      const stdoutDone = handleOutputStream(managed.proc.stdout);
      const stderrDone = handleOutputStream(managed.proc.stderr);

      const probeLoop = async () => {
        if (!hasNetworkProbe) return;
        const deadlineMs = startMs + task.readyTimeout * 1000;
        const targetsDesc = [
          task.readyTcp ? `tcp://${task.readyTcp}` : null,
          task.readyUrl || null,
        ]
          .filter(Boolean)
          .join(" & ");

        while (!stopRequested && !attemptDone) {
          const tcpOk = task.readyTcp ? await probeTcp(task.readyTcp) : true;
          const httpOk = task.readyUrl ? await probeHttp(task.readyUrl) : true;
          if (tcpOk && httpOk) {
            probeOk = true;
            writer.writeLine(name, style(`ready (${targetsDesc})`, GREEN, { enabled: useColor }));
            maybeMarkReady();
            return;
          }
          if (Date.now() >= deadlineMs) {
            probeTimedOut = true;
            depFailed[name] = true;
            writer.writeLine(
              name,
              style(
                `readiness check timed out after ${task.readyTimeout.toFixed(1)}s (${targetsDesc})`,
                RED,
                { enabled: useColor }
              )
            );
            readyGates[name].set();
            if (failFast && !isWatched && !stopRequested) {
              await stopAll(
                `Task '${name}' readiness check timed out — stopping remaining services...`,
                false
              );
            } else {
              await terminateManagedProcess(managed, shutdownTimeout);
            }
            return;
          }
          await sleep(150);
        }
      };

      const probePromise = probeLoop();

      let restartRequested = false;
      const watchIntervalMs = Math.max(50, Math.round(opts.watchInterval * 1000));
      let nextWatchPoll = Date.now() + watchIntervalMs;
      let nextDescPoll = Date.now() + 200;

      while (!managed.exited) {
        const now = Date.now();
        if (process.platform === "win32" && now >= nextDescPoll) {
          nextDescPoll = now + 500;
          scheduleDescendantSnapshot(managed);
        }
        if (stopRequested) {
          await terminateManagedProcess(managed, shutdownTimeout, forceKillRequested);
          break;
        }
        if (isWatched && now >= nextWatchPoll) {
          nextWatchPoll = now + watchIntervalMs;
          const newSnap = snapshotTree(watchDir, watchPatterns);
          const changed = diffSnapshots(snap, newSnap);
          if (changed) {
            await sleep(150);
            snap = snapshotTree(watchDir, watchPatterns);
            writer.writeSystem(`Change detected in ${changed.name} — restarting '${name}'...`, CYAN);
            restartRequested = true;
            await terminateManagedProcess(managed, shutdownTimeout);
            break;
          }
        }
        await Promise.race([managed.exitPromise, sleep(50)]);
      }

      attemptDone = true;
      await Promise.all([
        probePromise,
        Promise.race([stdoutDone, sleep(1000)]),
        Promise.race([stderrDone, sleep(1000)]),
      ]);

      activeProcs.delete(name);
      if (stopRequested) {
        await terminateManagedProcess(managed, shutdownTimeout, forceKillRequested);
      }

      let exitCode = managed.exitCode !== null ? managed.exitCode : 1;
      const duration = (Date.now() - startMs) / 1000;

      if (restartRequested && !stopRequested) {
        readyGates[name].set();
        env = buildTaskEnv(config, task, workspaceDotenv).env;
        continue;
      }

      let status;
      if (probeTimedOut) {
        status = "failed";
        exitCode = exitCode !== 0 ? exitCode : 1;
        depFailed[name] = true;
      } else if (interrupted || (stopRequested && exitCode !== 0)) {
        status = "stopped";
      } else if (exitCode === 0) {
        status = "ok";
      } else {
        status = "failed";
        if (!isWatched) {
          depFailed[name] = true;
        }
      }

      readyGates[name].set();
      resultsMap.set(
        name,
        createTaskResult({
          name,
          exitCode,
          durationSeconds: duration,
          status,
        })
      );

      if (status === "ok") {
        writer.writeLine(
          name,
          style(`exited with code 0 (${duration.toFixed(2)}s)`, GREEN, { enabled: useColor })
        );
      } else if (status === "stopped") {
        writer.writeLine(
          name,
          style(`stopped (${duration.toFixed(2)}s)`, YELLOW, { enabled: useColor })
        );
      } else {
        writer.writeLine(
          name,
          style(`exited with code ${exitCode} (${duration.toFixed(2)}s)`, RED, { enabled: useColor })
        );
        if (failFast && !isWatched && !stopRequested) {
          await stopAll(
            `Task '${name}' exited with code ${exitCode} — stopping remaining services...`,
            false
          );
        }
      }

      if (!isWatched || stopRequested) {
        break;
      }

      writer.writeLine(name, style("watching for file changes...", DIM, { enabled: useColor }));
      while (!stopRequested) {
        await sleep(watchIntervalMs);
        if (stopRequested) break;
        const newSnap = snapshotTree(watchDir, watchPatterns);
        const changed = diffSnapshots(snap, newSnap);
        if (changed) {
          await sleep(150);
          snap = snapshotTree(watchDir, watchPatterns);
          writer.writeSystem(`Change detected in ${changed.name} — restarting '${name}'...`, CYAN);
          env = buildTaskEnv(config, task, workspaceDotenv).env;
          break;
        }
      }

      if (stopRequested) break;
    }
  }

  try {
    await Promise.all(orderedNames.map((name) => runOne(name)));
    await Promise.all(stopPromises);
  } finally {
    process.off("SIGINT", handleSignal);
    process.off("SIGTERM", handleSignal);
    if (abortSignal) {
      abortSignal.removeEventListener("abort", onAbort);
    }
  }

  const orderedResults = orderedNames
    .filter((name) => resultsMap.has(name))
    .map((name) => resultsMap.get(name));
  printSummary(writer, orderedResults, useColor, interrupted);
  return orderedResults;
}
