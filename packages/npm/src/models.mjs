/**
 * Data factory helpers for tasks, workspace config, and task results.
 */

export function createTask({
  name,
  command,
  source,
  runner = "shell",
  description = "",
  cwd = null,
  env = {},
  dependsOn = [],
  readyPattern = null,
  readyUrl = null,
  readyTcp = null,
  readyTimeout = 30.0,
  aliases = [],
  workspace = null,
  envFiles = [],
  ports = [],
  watch = [],
  watchEnabled = false,
}) {
  return {
    name,
    command,
    source,
    runner,
    description,
    cwd,
    env: { ...env },
    dependsOn: [...dependsOn],
    readyPattern,
    readyUrl,
    readyTcp,
    readyTimeout,
    aliases: [...aliases],
    workspace,
    envFiles: [...envFiles],
    ports: [...ports],
    watch: [...watch],
    watchEnabled: Boolean(watchEnabled),
  };
}

export function createWorkspaceConfig(root) {
  return {
    root,
    tasks: {},
    groups: {},
    defaultTargets: [],
    env: {},
    envFiles: [],
    autoEnv: true,
    strictPorts: false,
    failFast: true,
    shutdownTimeout: 5.0,
    warnings: [],
  };
}

export function createTaskResult({
  name,
  exitCode,
  durationSeconds,
  status,
  error = null,
}) {
  return {
    name,
    exitCode,
    durationSeconds,
    status,
    error,
  };
}
