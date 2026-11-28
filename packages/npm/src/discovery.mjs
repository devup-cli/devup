import { existsSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { basename, join, relative, resolve } from "node:path";

import { expandGlobDirs, isDir, isFile, which } from "./fs_utils.mjs";
import { createTask, createWorkspaceConfig } from "./models.mjs";
import { normalizeTcpTarget, validateReadyUrl } from "./readiness.mjs";
import { parseToml } from "./toml.mjs";
import { extractPomMetadata } from "./xml.mjs";

const COMPOSE_FILES = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"];
const JUSTFILES = ["justfile", "Justfile", ".justfile"];
const MAKEFILES = ["GNUmakefile", "Makefile", "makefile"];
const GRADLE_FILES = ["build.gradle.kts", "build.gradle"];
const GRADLE_SETTINGS_FILES = ["settings.gradle.kts", "settings.gradle"];
const TASKFILES = ["Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"];
const MISE_FILES = ["mise.toml", ".mise.toml", ".config/mise.toml"];

const MAKE_TARGET_RE = /^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)(?:\s*[^#\n]*)(?:#+\s*(.*))?$/;
const JUST_RECIPE_RE = /^@?([A-Za-z][A-Za-z0-9_-]*)(?:\s+[^:=\n]+?)?\s*:\s*(?:#\s*(.*))?$/;
const GRADLE_REGISTER_RE = /tasks\.register(?:<[^>]+>)?\(\s*["']([A-Za-z0-9_-]+)["']/g;
const GRADLE_KTS_DELEGATE_RE = /val\s+([A-Za-z][A-Za-z0-9_-]*)\s+by\s+tasks\.(?:registering|creating)/g;
const GRADLE_GROOVY_TASK_RE = /^\s*task\s+([A-Za-z][A-Za-z0-9_-]*)\s*(?:\(|{|$)/;
const GRADLE_INCLUDE_RE = /include\s*\(?\s*([^\n)]+)/g;

function safeRealpath(p) {
  try {
    return realpathSync(p);
  } catch {
    return resolve(p);
  }
}

function registerTask(tasks, task, qualifier, allowOverride = false) {
  if (allowOverride || !(task.name in tasks)) {
    tasks[task.name] = task;
    return;
  }
  const qualifiedName = `${qualifier}:${task.name}`;
  if (!(qualifiedName in tasks)) {
    task.name = qualifiedName;
    tasks[qualifiedName] = task;
  }
}

function detectNodeRunner(directory, root) {
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    if (isFile(join(base, "pnpm-lock.yaml"))) return "pnpm";
    if (isFile(join(base, "yarn.lock"))) return "yarn";
    if (isFile(join(base, "bun.lockb")) || isFile(join(base, "bun.lock"))) return "bun";
    if (isFile(join(base, "package-lock.json"))) return "npm";
  }
  return "npm";
}

function discoverPackageJson(directory, root, prefix = "") {
  const pkgPath = join(directory, "package.json");
  if (!isFile(pkgPath)) {
    return { tasks: [], workspaces: [], warnings: [] };
  }

  let data;
  try {
    data = JSON.parse(readFileSync(pkgPath, "utf8"));
  } catch (err) {
    return { tasks: [], workspaces: [], warnings: [`Failed to parse ${pkgPath}: ${err.message}`] };
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { tasks: [], workspaces: [], warnings: [] };
  }

  const tasks = [];
  const runner = detectNodeRunner(directory, root);
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  if (data.scripts && typeof data.scripts === "object" && !Array.isArray(data.scripts)) {
    for (const [name, cmd] of Object.entries(data.scripts)) {
      if (typeof name !== "string" || typeof cmd !== "string") continue;
      if (name.startsWith("//") || !cmd.trim()) continue;
      const runCmd = runner === "yarn" ? `${runner} ${name}` : `${runner} run ${name}`;
      tasks.push(
        createTask({
          name: `${prefix}${name}`,
          command: runCmd,
          source: "package.json",
          runner,
          description: cmd.trim(),
          cwd: directory,
          workspace: workspaceName,
        })
      );
    }
  }

  let workspaceGlobs = [];
  if (Array.isArray(data.workspaces)) {
    workspaceGlobs = data.workspaces.filter((w) => typeof w === "string");
  } else if (data.workspaces && typeof data.workspaces === "object" && Array.isArray(data.workspaces.packages)) {
    workspaceGlobs = data.workspaces.packages.filter((w) => typeof w === "string");
  }

  return { tasks, workspaces: workspaceGlobs, warnings: [] };
}

function formatPoeOrPdmDesc(entry) {
  if (typeof entry === "string") return entry.trim();
  if (entry && typeof entry === "object" && !Array.isArray(entry)) {
    if (typeof entry.help === "string" && entry.help.trim()) {
      return entry.help.trim();
    }
    for (const key of ["cmd", "shell", "script", "composite"]) {
      const val = entry[key];
      if (typeof val === "string" && val.trim()) return val.trim();
      if (Array.isArray(val)) return val.map(String).join(" && ");
    }
  }
  return "";
}

function discoverPyproject(directory, prefix = "") {
  const pyprojectPath = join(directory, "pyproject.toml");
  if (!isFile(pyprojectPath)) {
    return { tasks: [], warnings: [] };
  }

  let data;
  try {
    data = parseToml(readFileSync(pyprojectPath, "utf8"));
  } catch (err) {
    return { tasks: [], warnings: [`Failed to parse ${pyprojectPath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const tool = data.tool && typeof data.tool === "object" ? data.tool : {};
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, command, runner, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command,
        source: "pyproject.toml",
        runner,
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  const poeTasks = tool.poe && typeof tool.poe === "object" ? tool.poe.tasks : null;
  if (poeTasks && typeof poeTasks === "object") {
    for (const [name, entry] of Object.entries(poeTasks)) {
      if (name.startsWith("_")) continue;
      addTask(name, `poe ${name}`, "poe", formatPoeOrPdmDesc(entry));
    }
  }

  const pdmScripts = tool.pdm && typeof tool.pdm === "object" ? tool.pdm.scripts : null;
  if (pdmScripts && typeof pdmScripts === "object") {
    for (const [name, entry] of Object.entries(pdmScripts)) {
      if (name.startsWith("_") || name === "_") continue;
      addTask(name, `pdm run ${name}`, "pdm", formatPoeOrPdmDesc(entry));
    }
  }

  const hatchEnvs = tool.hatch && typeof tool.hatch === "object" ? tool.hatch.envs : null;
  if (hatchEnvs && typeof hatchEnvs === "object") {
    for (const [envName, envCfg] of Object.entries(hatchEnvs)) {
      if (!envCfg || typeof envCfg !== "object") continue;
      const scripts = envCfg.scripts;
      if (!scripts || typeof scripts !== "object") continue;
      for (const [name, entry] of Object.entries(scripts)) {
        const targetName = envName === "default" ? name : `${envName}:${name}`;
        const hatchTarget = envName === "default" ? name : `${envName}:${name}`;
        const desc =
          typeof entry === "string"
            ? entry
            : Array.isArray(entry)
              ? entry.map(String).join(" | ")
              : "";
        addTask(targetName, `hatch run ${hatchTarget}`, "hatch", desc);
      }
    }
  }

  const poetryScripts = tool.poetry && typeof tool.poetry === "object" ? tool.poetry.scripts : null;
  if (poetryScripts && typeof poetryScripts === "object") {
    for (const [name, entry] of Object.entries(poetryScripts)) {
      const desc = typeof entry === "string" ? entry : String(entry);
      addTask(name, `poetry run ${name}`, "poetry", desc);
    }
  }

  const projectScripts = data.project && typeof data.project === "object" ? data.project.scripts : null;
  if (projectScripts && typeof projectScripts === "object") {
    for (const [name, entry] of Object.entries(projectScripts)) {
      if (typeof entry === "string") {
        addTask(name, name, "python", entry);
      }
    }
  }

  return { tasks, warnings: [] };
}

function discoverMakefile(directory, prefix = "") {
  const makefileName = MAKEFILES.find((n) => isFile(join(directory, n)));
  if (!makefileName) return { tasks: [], warnings: [] };
  const makefilePath = join(directory, makefileName);

  let lines;
  try {
    lines = readFileSync(makefilePath, "utf8").split(/\r?\n/);
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${makefilePath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  let prevComment = "";

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      prevComment = "";
      continue;
    }
    if (stripped.startsWith("#")) {
      prevComment = stripped.replace(/^#+/, "").trim();
      continue;
    }
    if (line.startsWith("\t") || line.startsWith(" ")) {
      prevComment = "";
      continue;
    }
    const match = MAKE_TARGET_RE.exec(line);
    if (match) {
      const target = match[1];
      const inlineDoc = (match[2] || "").trim();
      if (!target.startsWith(".") && !seen.has(target)) {
        seen.add(target);
        tasks.push(
          createTask({
            name: `${prefix}${target}`,
            command: `make ${target}`,
            source: makefileName,
            runner: "make",
            description: inlineDoc || prevComment,
            cwd: directory,
            workspace: workspaceName,
          })
        );
      }
    }
    prevComment = "";
  }

  return { tasks, warnings: [] };
}

function discoverJustfile(directory, prefix = "") {
  const justName = JUSTFILES.find((n) => isFile(join(directory, n)));
  if (!justName) return { tasks: [], warnings: [] };
  const justPath = join(directory, justName);

  let lines;
  try {
    lines = readFileSync(justPath, "utf8").split(/\r?\n/);
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${justPath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  let prevComment = "";

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      prevComment = "";
      continue;
    }
    if (stripped.startsWith("#")) {
      prevComment = stripped.replace(/^#+/, "").trim();
      continue;
    }
    if (line.startsWith("\t") || line.startsWith(" ")) {
      prevComment = "";
      continue;
    }
    if (/^(set |alias |export |import |mod )/.test(stripped)) {
      prevComment = "";
      continue;
    }
    const match = JUST_RECIPE_RE.exec(line);
    if (match) {
      const recipe = match[1];
      const inlineDoc = (match[2] || "").trim();
      if (!recipe.startsWith("_") && !seen.has(recipe)) {
        seen.add(recipe);
        tasks.push(
          createTask({
            name: `${prefix}${recipe}`,
            command: `just ${recipe}`,
            source: justName,
            runner: "just",
            description: inlineDoc || prevComment,
            cwd: directory,
            workspace: workspaceName,
          })
        );
      }
    }
    prevComment = "";
  }

  return { tasks, warnings: [] };
}

function parseComposeServices(text) {
  const services = [];
  let inServices = false;
  let serviceIndent = null;

  for (const rawLine of text.split(/\r?\n/)) {
    const trimStart = rawLine.trimStart();
    if (!trimStart || trimStart.startsWith("#")) continue;

    const indent = rawLine.length - trimStart.length;
    const stripped = rawLine.trim();

    if (indent === 0) {
      if (stripped.startsWith("services:")) {
        inServices = true;
        serviceIndent = null;
      } else {
        inServices = false;
      }
      continue;
    }

    if (!inServices) continue;
    if (serviceIndent === null) serviceIndent = indent;

    if (indent === serviceIndent && stripped.includes(":") && !stripped.startsWith("-")) {
      const key = stripped.split(":", 1)[0].trim().replace(/^["']|["']$/g, "");
      if (key) services.push(key);
    }
  }

  return services;
}

function detectComposeBinary() {
  if (which("docker")) return "docker compose";
  if (which("docker-compose")) return "docker-compose";
  return "docker compose";
}

function discoverCompose(directory, prefix = "") {
  const composeName = COMPOSE_FILES.find((n) => isFile(join(directory, n)));
  if (!composeName) return { tasks: [], warnings: [] };
  const composePath = join(directory, composeName);

  let content;
  try {
    content = readFileSync(composePath, "utf8");
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${composePath}: ${err.message}`] };
  }

  const services = parseComposeServices(content);
  if (services.length === 0) return { tasks: [], warnings: [] };

  const composeCmd = detectComposeBinary();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  const tasks = [
    createTask({
      name: `${prefix}compose:up`,
      command: `${composeCmd} up`,
      source: composeName,
      runner: "docker-compose",
      description: `Start all services (${services.join(", ")})`,
      cwd: directory,
      workspace: workspaceName,
    }),
    createTask({
      name: `${prefix}compose:down`,
      command: `${composeCmd} down`,
      source: composeName,
      runner: "docker-compose",
      description: "Stop and remove compose containers",
      cwd: directory,
      workspace: workspaceName,
    }),
  ];

  for (const svc of services) {
    tasks.push(
      createTask({
        name: `${prefix}compose:${svc}`,
        command: `${composeCmd} up ${svc}`,
        source: composeName,
        runner: "docker-compose",
        description: `Start Docker Compose service '${svc}'`,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  return { tasks, warnings: [] };
}

function formatWrapperCmd(wrapperPath, directory) {
  let rel = relative(directory, wrapperPath);
  if (!rel) rel = basename(wrapperPath);
  if (process.platform === "win32") {
    return rel.includes(" ") ? `"${rel}"` : rel;
  }
  if (!rel.startsWith(".") && !rel.startsWith("/")) {
    rel = `./${rel}`;
  }
  return rel.includes(" ") ? `"${rel}"` : rel;
}

function detectMavenRunner(directory, root) {
  const wrapperName = process.platform === "win32" ? "mvnw.cmd" : "mvnw";
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    const candidate = join(base, wrapperName);
    if (isFile(candidate)) return formatWrapperCmd(candidate, directory);
  }
  return "mvn";
}

function discoverMaven(directory, root, prefix = "") {
  const pomPath = join(directory, "pom.xml");
  if (!isFile(pomPath)) return { tasks: [], modules: [], warnings: [] };

  let meta;
  try {
    meta = extractPomMetadata(readFileSync(pomPath, "utf8"));
  } catch (err) {
    return { tasks: [], modules: [], warnings: [`Failed to parse ${pomPath}: ${err.message}`] };
  }

  const mvn = detectMavenRunner(directory, root);
  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, goal, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: `${mvn} ${goal}`,
        source: "pom.xml",
        runner: "maven",
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  for (const artifact of meta.artifactIds) {
    if (artifact === "spring-boot-maven-plugin") {
      addTask("dev", "spring-boot:run", "Run Spring Boot application");
    } else if (artifact === "quarkus-maven-plugin") {
      addTask("dev", "quarkus:dev", "Run Quarkus in dev mode");
    } else if (artifact === "exec-maven-plugin") {
      addTask("exec", "exec:java", "Run main class via exec-maven-plugin");
    }
  }

  for (const [goal, desc] of [
    ["compile", "Compile project sources"],
    ["test", "Run Maven tests"],
    ["package", "Package Maven artifact"],
    ["verify", "Run verification checks"],
    ["clean", "Clean Maven target directory"],
  ]) {
    addTask(goal, goal, desc);
  }

  return { tasks, modules: meta.modules, warnings: [] };
}

function detectGradleRunner(directory, root) {
  const wrapperName = process.platform === "win32" ? "gradlew.bat" : "gradlew";
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    const candidate = join(base, wrapperName);
    if (isFile(candidate)) return formatWrapperCmd(candidate, directory);
  }
  return "gradle";
}

function discoverGradleSubprojects(directory) {
  const settingsName = GRADLE_SETTINGS_FILES.find((n) => isFile(join(directory, n)));
  if (!settingsName) return [];

  let text;
  try {
    text = readFileSync(join(directory, settingsName), "utf8");
  } catch {
    return [];
  }

  const subprojects = [];
  const seen = new Set();
  GRADLE_INCLUDE_RE.lastIndex = 0;
  let match;
  while ((match = GRADLE_INCLUDE_RE.exec(text)) !== null) {
    const blob = match[1];
    const quoteRe = /["']([^"']+)["']/g;
    let qm;
    while ((qm = quoteRe.exec(blob)) !== null) {
      const rel = qm[1].replace(/^:+/, "").replaceAll(":", "/").trim();
      if (rel && !seen.has(rel)) {
        seen.add(rel);
        subprojects.push(rel);
      }
    }
  }
  return subprojects;
}

function discoverGradle(directory, root, prefix = "") {
  const gradleName = GRADLE_FILES.find((n) => isFile(join(directory, n)));
  const subprojects = discoverGradleSubprojects(directory);
  if (!gradleName && subprojects.length === 0) {
    return { tasks: [], subprojects: [], warnings: [] };
  }

  const gradle = detectGradleRunner(directory, root);
  const sourceName = gradleName || "settings.gradle";
  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, gradleTask, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: `${gradle} ${gradleTask}`,
        source: sourceName,
        runner: "gradle",
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  if (gradleName) {
    const gradlePath = join(directory, gradleName);
    let content;
    try {
      content = readFileSync(gradlePath, "utf8");
    } catch (err) {
      return { tasks: [], subprojects, warnings: [`Failed to read ${gradlePath}: ${err.message}`] };
    }

    if (content.includes("org.springframework.boot")) {
      addTask("bootRun", "bootRun", "Run Spring Boot application");
    }
    if (/\bapplication\b|\bmainClass\b/.test(content)) {
      addTask("run", "run", "Run Gradle application");
    }

    for (const regex of [GRADLE_REGISTER_RE, GRADLE_KTS_DELEGATE_RE]) {
      regex.lastIndex = 0;
      let m;
      while ((m = regex.exec(content)) !== null) {
        addTask(m[1], m[1], `Custom Gradle task '${m[1]}'`);
      }
    }
    for (const line of content.split(/\r?\n/)) {
      const m = GRADLE_GROOVY_TASK_RE.exec(line);
      if (m) {
        addTask(m[1], m[1], `Custom Gradle task '${m[1]}'`);
      }
    }
  }

  for (const [tname, desc] of [
    ["build", "Assemble and test Gradle project"],
    ["test", "Run Gradle tests"],
    ["check", "Run Gradle verification checks"],
    ["clean", "Clean Gradle build directory"],
  ]) {
    addTask(tname, tname, desc);
  }

  return { tasks, subprojects, warnings: [] };
}

function discoverCargo(directory, root, prefix = "") {
  const cargoPath = join(directory, "Cargo.toml");
  if (!isFile(cargoPath)) return { tasks: [], members: [], warnings: [] };

  let data;
  try {
    data = parseToml(readFileSync(cargoPath, "utf8"));
  } catch (err) {
    return {
      tasks: [],
      members: [],
      warnings: [`Failed to parse ${cargoPath} in repo ${root}: ${err.message}`],
    };
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { tasks: [], members: [], warnings: [] };
  }

  const tasks = [];
  const seen = new Set();
  const wsName = prefix ? prefix.replace(/:+$/, "") : null;
  const rawWs = data.workspace && typeof data.workspace === "object" ? data.workspace : null;
  const members =
    rawWs && Array.isArray(rawWs.members)
      ? rawWs.members.map(String).filter((m) => !m.startsWith("!"))
      : [];

  function addTask(name, cargoCmd, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: cargoCmd,
        source: "Cargo.toml",
        runner: "cargo",
        description,
        cwd: directory,
        workspace: wsName,
      })
    );
  }

  const pkg = data.package && typeof data.package === "object" ? data.package : null;
  const bins = Array.isArray(data.bin) ? data.bin : [];
  for (const entry of bins) {
    if (entry && typeof entry === "object" && typeof entry.name === "string" && entry.name.trim()) {
      const bname = entry.name.trim();
      addTask(`run:${bname}`, `cargo run --bin ${bname}`, `Run Cargo binary '${bname}'`);
    }
  }

  if (pkg) {
    const hasMain = isFile(join(directory, "src", "main.rs"));
    const hasLib = isFile(join(directory, "src", "lib.rs"));
    if (hasMain || bins.length > 0 || !hasLib) {
      addTask("run", "cargo run", `Run Cargo package '${pkg.name || basename(directory)}'`);
    }
    for (const [sub, desc] of [
      ["build", "Build Cargo package"],
      ["test", "Run Cargo tests"],
      ["check", "Type-check Cargo package"],
      ["clippy", "Run Clippy lints"],
    ]) {
      addTask(sub, `cargo ${sub}`, desc);
    }
  } else if (rawWs) {
    for (const [sub, desc] of [
      ["build", "Build Cargo workspace"],
      ["test", "Run Cargo workspace tests"],
      ["check", "Check Cargo workspace"],
    ]) {
      addTask(sub, `cargo ${sub} --workspace`, desc);
    }
  }

  return { tasks, members, warnings: [] };
}

function parseGoWorkModules(workPath) {
  let lines;
  try {
    lines = readFileSync(workPath, "utf8").split(/\r?\n/);
  } catch {
    return [];
  }
  const modules = [];
  let inUse = false;
  for (const raw of lines) {
    const line = raw.split("//", 1)[0].trim();
    if (!line) continue;
    if (line.startsWith("use (")) {
      inUse = true;
      continue;
    }
    if (inUse) {
      if (line.startsWith(")")) {
        inUse = false;
        continue;
      }
      const mod = line.trim().replace(/^"|"$/g, "").replace(/^\.\//, "");
      if (mod && mod !== "." && !modules.includes(mod)) modules.push(mod);
    } else if (line.startsWith("use ")) {
      const mod = line.slice(4).trim().replace(/^"|"$/g, "").replace(/^\.\//, "");
      if (mod && mod !== "." && !modules.includes(mod)) modules.push(mod);
    }
  }
  return modules;
}

function discoverGo(directory, root, prefix = "") {
  const gomodPath = join(directory, "go.mod");
  const goworkPath = join(directory, "go.work");
  const hasGomod = isFile(gomodPath);
  const workModules = isFile(goworkPath) ? parseGoWorkModules(goworkPath) : [];
  if (!hasGomod && workModules.length === 0) return { tasks: [], modules: [], warnings: [] };

  const sourceName = hasGomod ? "go.mod" : "go.work";
  if (hasGomod) {
    let modText;
    try {
      modText = readFileSync(gomodPath, "utf8");
    } catch (err) {
      return {
        tasks: [],
        modules: workModules,
        warnings: [`Failed to read ${gomodPath} in repo ${root}: ${err.message}`],
      };
    }
    if (!modText.includes("module ")) {
      return {
        tasks: [],
        modules: workModules,
        warnings: [`Malformed go.mod in repo ${root} (${gomodPath}): missing 'module' directive.`],
      };
    }
  }

  const tasks = [];
  const seen = new Set();
  const wsName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, cmd, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: cmd,
        source: sourceName,
        runner: "go",
        description,
        cwd: directory,
        workspace: wsName,
      })
    );
  }

  const cmdDir = join(directory, "cmd");
  let hasCmdBins = false;
  if (isDir(cmdDir)) {
    let entries = [];
    try {
      entries = readdirSync(cmdDir, { withFileTypes: true }).sort((a, b) =>
        a.name.localeCompare(b.name)
      );
    } catch {
      entries = [];
    }
    for (const child of entries) {
      if (child.isDirectory() && isFile(join(cmdDir, child.name, "main.go"))) {
        hasCmdBins = true;
        addTask(`run:${child.name}`, `go run ./cmd/${child.name}`, `Run Go command './cmd/${child.name}'`);
      }
    }
  }

  if (isFile(join(directory, "main.go")) || !hasCmdBins) {
    addTask("run", "go run .", "Run Go main package");
  }

  for (const [name, goCmd, desc] of [
    ["build", "go build ./...", "Build Go packages"],
    ["test", "go test ./...", "Run Go tests"],
    ["vet", "go vet ./...", "Run go vet static analyzer"],
  ]) {
    addTask(name, goCmd, desc);
  }

  return { tasks, modules: workModules, warnings: [] };
}

function parseTaskfileYaml(text) {
  const tasks = new Map();
  let inTasks = false;
  let taskIndent = null;
  let currentTask = null;
  let propIndent = null;

  for (const rawLine of text.split(/\r?\n/)) {
    const strippedComment = rawLine.split("#", 1)[0].trimEnd();
    if (!strippedComment.trim()) continue;
    const indent = rawLine.length - rawLine.trimStart().length;
    const stripped = strippedComment.trim();

    if (indent === 0) {
      if (stripped === "tasks:" || stripped.startsWith("tasks:")) {
        inTasks = true;
        taskIndent = null;
        currentTask = null;
        propIndent = null;
      } else {
        inTasks = false;
        currentTask = null;
      }
      continue;
    }

    if (!inTasks) continue;
    if (taskIndent === null) taskIndent = indent;

    if (indent === taskIndent && stripped.includes(":") && !stripped.startsWith("-")) {
      const key = stripped.split(":", 1)[0].trim().replace(/^["']|["']$/g, "");
      currentTask = key || null;
      propIndent = null;
      if (currentTask && !tasks.has(currentTask)) {
        tasks.set(currentTask, "");
      }
      continue;
    }

    if (currentTask !== null && indent > taskIndent) {
      if (propIndent === null) propIndent = indent;
      if (indent === propIndent) {
        if (stripped.startsWith("desc:") || stripped.startsWith("summary:")) {
          const idx = stripped.indexOf(":");
          const val = stripped.slice(idx + 1).trim().replace(/^["']|["']$/g, "");
          if (val && !tasks.get(currentTask)) tasks.set(currentTask, val);
        } else if (stripped.startsWith("internal:")) {
          const val = stripped.slice(stripped.indexOf(":") + 1).trim().toLowerCase();
          if (val === "true") {
            tasks.delete(currentTask);
            currentTask = null;
          }
        }
      }
    }
  }
  return tasks;
}

function discoverTaskfile(directory, root, prefix = "") {
  const taskfileName = TASKFILES.find((n) => isFile(join(directory, n)));
  if (!taskfileName) return { tasks: [], warnings: [] };
  const taskfilePath = join(directory, taskfileName);

  let content;
  try {
    content = readFileSync(taskfilePath, "utf8");
  } catch (err) {
    return {
      tasks: [],
      warnings: [`Failed to read ${taskfilePath} in repo ${root}: ${err.message}`],
    };
  }

  const parsed = parseTaskfileYaml(content);
  const tasks = [];
  const wsName = prefix ? prefix.replace(/:+$/, "") : null;
  for (const [name, desc] of parsed.entries()) {
    tasks.push(
      createTask({
        name: `${prefix}${name}`,
        command: `task ${name}`,
        source: taskfileName,
        runner: "task",
        description: desc,
        cwd: directory,
        workspace: wsName,
      })
    );
  }
  return { tasks, warnings: [] };
}

function discoverMise(directory, root, prefix = "") {
  const miseRel = MISE_FILES.find((n) => isFile(join(directory, n)));
  if (!miseRel) return { tasks: [], warnings: [] };
  const misePath = join(directory, miseRel);

  let data;
  try {
    data = parseToml(readFileSync(misePath, "utf8"));
  } catch (err) {
    return {
      tasks: [],
      warnings: [`Failed to parse ${misePath} in repo ${root}: ${err.message}`],
    };
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) return { tasks: [], warnings: [] };
  const rawTasks = data.tasks;
  if (!rawTasks || typeof rawTasks !== "object" || Array.isArray(rawTasks)) {
    return { tasks: [], warnings: [] };
  }

  const tasks = [];
  const wsName = prefix ? prefix.replace(/:+$/, "") : null;
  for (const [name, entry] of Object.entries(rawTasks)) {
    if (name.startsWith("_")) continue;
    let desc = "";
    if (typeof entry === "string") {
      desc = entry.trim();
    } else if (entry && typeof entry === "object" && !Array.isArray(entry)) {
      if (entry.hide === true) continue;
      if (typeof entry.description === "string" && entry.description.trim()) {
        desc = entry.description.trim();
      } else if (typeof entry.run === "string") {
        desc = entry.run.trim();
      } else if (Array.isArray(entry.run)) {
        desc = entry.run.map(String).join(" && ");
      }
    } else {
      continue;
    }

    tasks.push(
      createTask({
        name: `${prefix}${name}`,
        command: `mise run ${name}`,
        source: miseRel,
        runner: "mise",
        description: desc,
        cwd: directory,
        workspace: wsName,
      })
    );
  }
  return { tasks, warnings: [] };
}

function resolveWorkspaceDirs(root, patterns) {
  const dirs = [];
  const seen = new Set();
  const resolvedRootReal = safeRealpath(root);

  for (const pattern of patterns) {
    const candidates = expandGlobDirs(root, pattern);
    for (const cand of candidates) {
      const realCand = safeRealpath(cand);
      if (realCand !== resolvedRootReal && !seen.has(realCand)) {
        seen.add(realCand);
        dirs.push(cand);
      }
    }
  }
  return dirs;
}

function discoverDirectory(directory, root, tasks, warnings, prefix = "", debugEvents = null) {
  const discoveredWorkspaces = [];
  const log = (msg) => {
    if (debugEvents) debugEvents.push(msg);
  };

  const pkg = discoverPackageJson(directory, root, prefix);
  warnings.push(...pkg.warnings);
  discoveredWorkspaces.push(...pkg.workspaces);
  if (pkg.tasks.length > 0) log(`discovery: ${join(directory, "package.json")} -> ${pkg.tasks.length} task(s)`);
  for (const task of pkg.tasks) registerTask(tasks, task, `${prefix}node`);

  const py = discoverPyproject(directory, prefix);
  warnings.push(...py.warnings);
  if (py.tasks.length > 0) log(`discovery: ${join(directory, "pyproject.toml")} -> ${py.tasks.length} task(s)`);
  for (const task of py.tasks) registerTask(tasks, task, `${prefix}py`);

  const crg = discoverCargo(directory, root, prefix);
  warnings.push(...crg.warnings);
  discoveredWorkspaces.push(...crg.members);
  if (crg.tasks.length > 0) log(`discovery: ${join(directory, "Cargo.toml")} -> ${crg.tasks.length} task(s)`);
  for (const task of crg.tasks) registerTask(tasks, task, `${prefix}cargo`);

  const go = discoverGo(directory, root, prefix);
  warnings.push(...go.warnings);
  discoveredWorkspaces.push(...go.modules);
  if (go.tasks.length > 0) log(`discovery: ${directory} (Go) -> ${go.tasks.length} task(s)`);
  for (const task of go.tasks) registerTask(tasks, task, `${prefix}go`);

  const mvn = discoverMaven(directory, root, prefix);
  warnings.push(...mvn.warnings);
  discoveredWorkspaces.push(...mvn.modules);
  if (mvn.tasks.length > 0) log(`discovery: ${join(directory, "pom.xml")} -> ${mvn.tasks.length} task(s)`);
  for (const task of mvn.tasks) registerTask(tasks, task, `${prefix}mvn`);

  const grad = discoverGradle(directory, root, prefix);
  warnings.push(...grad.warnings);
  discoveredWorkspaces.push(...grad.subprojects);
  if (grad.tasks.length > 0) log(`discovery: ${directory} (Gradle) -> ${grad.tasks.length} task(s)`);
  for (const task of grad.tasks) registerTask(tasks, task, `${prefix}gradle`);

  const tsk = discoverTaskfile(directory, root, prefix);
  warnings.push(...tsk.warnings);
  if (tsk.tasks.length > 0) log(`discovery: ${directory} (${tsk.tasks[0].source}) -> ${tsk.tasks.length} task(s)`);
  for (const task of tsk.tasks) registerTask(tasks, task, `${prefix}task`);

  const mse = discoverMise(directory, root, prefix);
  warnings.push(...mse.warnings);
  if (mse.tasks.length > 0) log(`discovery: ${directory} (${mse.tasks[0].source}) -> ${mse.tasks.length} task(s)`);
  for (const task of mse.tasks) registerTask(tasks, task, `${prefix}mise`);

  const jst = discoverJustfile(directory, prefix);
  warnings.push(...jst.warnings);
  if (jst.tasks.length > 0) log(`discovery: ${directory} (${jst.tasks[0].source}) -> ${jst.tasks.length} task(s)`);
  for (const task of jst.tasks) registerTask(tasks, task, `${prefix}just`);

  const mk = discoverMakefile(directory, prefix);
  warnings.push(...mk.warnings);
  if (mk.tasks.length > 0) log(`discovery: ${directory} (${mk.tasks[0].source}) -> ${mk.tasks.length} task(s)`);
  for (const task of mk.tasks) registerTask(tasks, task, `${prefix}make`);

  const cmp = discoverCompose(directory, prefix);
  warnings.push(...cmp.warnings);
  if (cmp.tasks.length > 0) log(`discovery: ${directory} (${cmp.tasks[0].source}) -> ${cmp.tasks.length} task(s)`);
  for (const task of cmp.tasks) registerTask(tasks, task, `${prefix}compose`);

  return discoveredWorkspaces;
}

function loadDevupToml(root, config) {
  const tomlPath = join(root, "devup.toml");
  if (!isFile(tomlPath)) return [];

  let data;
  try {
    data = parseToml(readFileSync(tomlPath, "utf8"));
  } catch (err) {
    config.warnings.push(`Failed to parse ${tomlPath}: ${err.message}`);
    return [];
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) return [];

  if (typeof data.fail_fast === "boolean") config.failFast = data.fail_fast;
  if (typeof data.auto_env === "boolean") config.autoEnv = data.auto_env;
  if (typeof data.strict_ports === "boolean") config.strictPorts = data.strict_ports;
  if (typeof data.shutdown_timeout === "number" && Number.isFinite(data.shutdown_timeout)) {
    config.shutdownTimeout = Math.max(0.5, data.shutdown_timeout);
  }

  if (typeof data.env_file === "string") {
    config.envFiles = [data.env_file];
  } else if (Array.isArray(data.env_file)) {
    config.envFiles = data.env_file.filter((f) => typeof f === "string");
  }

  if (Array.isArray(data.default)) {
    config.defaultTargets = data.default.filter((d) => typeof d === "string");
  } else if (typeof data.default === "string") {
    config.defaultTargets = [data.default];
  }

  if (data.env && typeof data.env === "object" && !Array.isArray(data.env)) {
    config.env = Object.fromEntries(Object.entries(data.env).map(([k, v]) => [String(k), String(v)]));
  }

  if (data.groups && typeof data.groups === "object" && !Array.isArray(data.groups)) {
    for (const [groupName, members] of Object.entries(data.groups)) {
      if (Array.isArray(members)) {
        config.groups[groupName] = members.filter((m) => typeof m === "string");
      }
    }
  }

  if (data.tasks && typeof data.tasks === "object" && !Array.isArray(data.tasks)) {
    for (const [taskName, entry] of Object.entries(data.tasks)) {
      if (typeof entry === "string") {
        registerTask(
          config.tasks,
          createTask({
            name: taskName,
            command: entry,
            source: "devup.toml",
            runner: "devup",
            cwd: root,
          }),
          "devup",
          true
        );
      } else if (entry && typeof entry === "object" && !Array.isArray(entry) && typeof entry.command === "string") {
        const taskCwd = typeof entry.cwd === "string" ? resolve(root, entry.cwd) : root;
        const taskEnv =
          entry.env && typeof entry.env === "object" && !Array.isArray(entry.env)
            ? Object.fromEntries(Object.entries(entry.env).map(([k, v]) => [String(k), String(v)]))
            : {};
        const deps = Array.isArray(entry.depends_on)
          ? entry.depends_on.filter((d) => typeof d === "string")
          : [];
        const readyPattern = typeof entry.ready_pattern === "string" ? entry.ready_pattern : null;

        let readyUrl = null;
        if (typeof entry.ready_url === "string") {
          try {
            readyUrl = validateReadyUrl(entry.ready_url);
          } catch (err) {
            config.warnings.push(`Task '${taskName}': ${err.message}`);
          }
        }

        let readyTcp = null;
        const rawTcp = "ready_tcp" in entry ? entry.ready_tcp : entry.ready_port;
        if (typeof rawTcp === "string" || typeof rawTcp === "number") {
          try {
            readyTcp = normalizeTcpTarget(rawTcp);
          } catch (err) {
            config.warnings.push(`Task '${taskName}': ${err.message}`);
          }
        }

        const readyTimeout =
          typeof entry.ready_timeout === "number" && Number.isFinite(entry.ready_timeout)
            ? Math.max(0.5, entry.ready_timeout)
            : 30.0;

        const description = typeof entry.description === "string" ? entry.description : "";

        let taskEnvFiles = [];
        if (typeof entry.env_file === "string") {
          taskEnvFiles = [entry.env_file];
        } else if (Array.isArray(entry.env_file)) {
          taskEnvFiles = entry.env_file.filter((f) => typeof f === "string");
        }

        let taskPorts = [];
        if (typeof entry.port === "number" && Number.isInteger(entry.port)) {
          taskPorts = [entry.port];
        } else if (Array.isArray(entry.ports)) {
          taskPorts = entry.ports.filter((p) => typeof p === "number" && Number.isInteger(p));
        }

        let watchEnabled = false;
        let watchPatterns = [];
        if (typeof entry.watch === "boolean") {
          watchEnabled = entry.watch;
        } else if (typeof entry.watch === "string") {
          watchEnabled = true;
          watchPatterns = [entry.watch];
        } else if (Array.isArray(entry.watch)) {
          watchPatterns = entry.watch.filter((w) => typeof w === "string");
          watchEnabled = watchPatterns.length > 0;
        }

        registerTask(
          config.tasks,
          createTask({
            name: taskName,
            command: entry.command,
            source: "devup.toml",
            runner: "devup",
            description,
            cwd: taskCwd,
            env: taskEnv,
            dependsOn: deps,
            readyPattern,
            readyUrl,
            readyTcp,
            readyTimeout,
            envFiles: taskEnvFiles,
            ports: taskPorts,
            watch: watchPatterns,
            watchEnabled,
          }),
          "devup",
          true
        );
      }
    }
  }

  if (Array.isArray(data.workspaces)) {
    return data.workspaces.filter((w) => typeof w === "string");
  }
  return [];
}

export function discoverWorkspace(root, scanWorkspaces = true) {
  const resolvedRoot = resolve(root);
  const config = createWorkspaceConfig(resolvedRoot);
  config.debugEvents.push(`discovery: scanning repository root ${resolvedRoot}`);

  const pkgWorkspaces = discoverDirectory(
    resolvedRoot,
    resolvedRoot,
    config.tasks,
    config.warnings,
    "",
    config.debugEvents
  );
  const tomlWorkspaces = loadDevupToml(resolvedRoot, config);

  if (scanWorkspaces) {
    const workspaceGlobs = tomlWorkspaces.length > 0 ? tomlWorkspaces : pkgWorkspaces;
    for (const wsDir of resolveWorkspaceDirs(resolvedRoot, workspaceGlobs)) {
      const wsName = basename(wsDir);
      config.debugEvents.push(`discovery: scanning workspace '${wsName}' at ${wsDir}`);
      discoverDirectory(
        wsDir,
        resolvedRoot,
        config.tasks,
        config.warnings,
        `${wsName}:`,
        config.debugEvents
      );
    }
    if (tomlWorkspaces.length > 0 || pkgWorkspaces.length > 0) {
      loadDevupToml(resolvedRoot, config);
    }
  }

  return config;
}

function similarityRatio(a, b) {
  if (a === b) return 1;
  if (!a || !b) return 0;
  const la = a.length;
  const lb = b.length;
  const dp = Array.from({ length: la + 1 }, () => new Array(lb + 1).fill(0));
  for (let i = 1; i <= la; i += 1) {
    for (let j = 1; j <= lb; j += 1) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }
  const lcs = dp[la][lb];
  return (2 * lcs) / (la + lb);
}

function getCloseMatches(word, possibilities, n = 3, cutoff = 0.45) {
  const scored = [];
  for (const candidate of possibilities) {
    const score = similarityRatio(word, candidate);
    if (score >= cutoff) {
      scored.push({ candidate, score });
    }
  }
  scored.sort((x, y) => y.score - x.score);
  return scored.slice(0, n).map((s) => s.candidate);
}

export function resolveTargets(config, targets) {
  const expanded = [];
  const seen = new Set();

  function expandOne(name, stack) {
    if (stack.has(name)) {
      throw new Error(`Circular group reference detected: ${name}`);
    }
    if (Object.hasOwn(config.groups, name)) {
      stack.add(name);
      for (const member of config.groups[name]) {
        expandOne(member, stack);
      }
      stack.delete(name);
      return;
    }
    if (!Object.hasOwn(config.tasks, name)) {
      const allNames = [...Object.keys(config.tasks), ...Object.keys(config.groups)].sort();
      const suggestions = getCloseMatches(name, allNames, 3, 0.45);
      const hint = suggestions.length > 0 ? ` Did you mean: ${suggestions.join(", ")}?` : "";
      const available = allNames.length > 0 ? allNames.join(", ") : "none";
      const err = new Error(
        `Unknown task or group '${name}'.${hint} Available: ${available}. Run \`devup list\` to inspect discovered targets.`
      );
      err.code = "ERR_UNKNOWN_TARGET";
      throw err;
    }
    if (!seen.has(name)) {
      seen.add(name);
      expanded.push(name);
    }
  }

  for (const target of targets) {
    expandOne(target, new Set());
  }

  return expanded;
}
