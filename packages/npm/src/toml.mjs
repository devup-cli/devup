/**
 * Lightweight zero-dependency TOML parser covering tables, arrays of tables,
 * inline tables, arrays, strings (basic/literal/multiline), numbers, and booleans.
 */

export class TomlParseError extends Error {
  constructor(message, line = 1) {
    super(`${message} (at line ${line})`);
    this.name = "TomlParseError";
    this.line = line;
  }
}

function isBareKeyChar(ch) {
  return /[A-Za-z0-9_-]/.test(ch);
}

export function parseToml(input) {
  const text = input.charCodeAt(0) === 0xfeff ? input.slice(1) : input;
  const len = text.length;
  let pos = 0;
  let line = 1;

  const root = {};
  let currentTable = root;
  // Track paths explicitly defined as headers/tables or immutable values
  const explicitTables = new Set();
  const arrayTables = new Set();

  function error(msg) {
    return new TomlParseError(msg, line);
  }

  function peek(offset = 0) {
    return pos + offset < len ? text[pos + offset] : "";
  }

  function advance(count = 1) {
    for (let i = 0; i < count; i += 1) {
      if (pos < len) {
        if (text[pos] === "\n") line += 1;
        pos += 1;
      }
    }
  }

  function skipHorizontalWs() {
    while (pos < len && (text[pos] === " " || text[pos] === "\t")) {
      pos += 1;
    }
  }

  function skipComment() {
    if (text[pos] === "#") {
      while (pos < len && text[pos] !== "\n") {
        pos += 1;
      }
    }
  }

  function skipWsAndComments() {
    while (pos < len) {
      const ch = text[pos];
      if (ch === " " || ch === "\t" || ch === "\r" || ch === "\n") {
        advance(1);
      } else if (ch === "#") {
        skipComment();
      } else {
        break;
      }
    }
  }

  function expectLineEndOrEof() {
    skipHorizontalWs();
    skipComment();
    if (pos >= len) return;
    if (text[pos] === "\r") {
      pos += 1;
    }
    if (text[pos] === "\n") {
      advance(1);
      return;
    }
    if (pos < len) {
      throw error(`Unexpected character '${text[pos]}' after expression`);
    }
  }

  function parseBasicString(multiline) {
    if (multiline && (peek() === "\n" || (peek() === "\r" && peek(1) === "\n"))) {
      if (peek() === "\r") advance(1);
      advance(1);
    }
    let out = "";
    while (pos < len) {
      const ch = text[pos];
      if (multiline && ch === '"' && peek(1) === '"' && peek(2) === '"') {
        let quoteCount = 0;
        while (peek(quoteCount) === '"') quoteCount += 1;
        if (quoteCount >= 3 && quoteCount <= 5) {
          out += '"'.repeat(quoteCount - 3);
          advance(quoteCount);
          return out;
        }
        if (quoteCount > 5) {
          throw error("Too many consecutive quotes in multiline string");
        }
      }
      if (!multiline && ch === '"') {
        advance(1);
        return out;
      }
      if (!multiline && (ch === "\n" || ch === "\r")) {
        throw error("Unterminated basic string");
      }
      if (ch === "\\") {
        advance(1);
        const esc = text[pos];
        if (!esc) throw error("Unterminated escape sequence");
        if (multiline && (esc === "\n" || esc === "\r" || esc === " " || esc === "\t")) {
          // Line-ending backslash trims newline and whitespace
          while (pos < len && (text[pos] === " " || text[pos] === "\t" || text[pos] === "\r")) {
            advance(1);
          }
          if (text[pos] !== "\n") {
            throw error("Invalid escape sequence in multiline string");
          }
          while (
            pos < len &&
            (text[pos] === " " || text[pos] === "\t" || text[pos] === "\r" || text[pos] === "\n")
          ) {
            advance(1);
          }
          continue;
        }
        advance(1);
        switch (esc) {
          case "b":
            out += "\b";
            break;
          case "t":
            out += "\t";
            break;
          case "n":
            out += "\n";
            break;
          case "f":
            out += "\f";
            break;
          case "r":
            out += "\r";
            break;
          case '"':
            out += '"';
            break;
          case "\\":
            out += "\\";
            break;
          case "u": {
            const hex = text.slice(pos, pos + 4);
            if (!/^[0-9A-Fa-f]{4}$/.test(hex)) throw error("Invalid \\uXXXX escape");
            out += String.fromCodePoint(parseInt(hex, 16));
            advance(4);
            break;
          }
          case "U": {
            const hex = text.slice(pos, pos + 8);
            if (!/^[0-9A-Fa-f]{8}$/.test(hex)) throw error("Invalid \\UXXXXXXXX escape");
            out += String.fromCodePoint(parseInt(hex, 16));
            advance(8);
            break;
          }
          default:
            throw error(`Invalid escape '\\${esc}'`);
        }
      } else {
        out += ch;
        advance(1);
      }
    }
    throw error("Unterminated string literal");
  }

  function parseLiteralString(multiline) {
    if (multiline && (peek() === "\n" || (peek() === "\r" && peek(1) === "\n"))) {
      if (peek() === "\r") advance(1);
      advance(1);
    }
    let out = "";
    while (pos < len) {
      const ch = text[pos];
      if (multiline && ch === "'" && peek(1) === "'" && peek(2) === "'") {
        let quoteCount = 0;
        while (peek(quoteCount) === "'") quoteCount += 1;
        if (quoteCount >= 3 && quoteCount <= 5) {
          out += "'".repeat(quoteCount - 3);
          advance(quoteCount);
          return out;
        }
        if (quoteCount > 5) {
          throw error("Too many consecutive apostrophes in multiline literal string");
        }
      }
      if (!multiline && ch === "'") {
        advance(1);
        return out;
      }
      if (!multiline && (ch === "\n" || ch === "\r")) {
        throw error("Unterminated literal string");
      }
      out += ch;
      advance(1);
    }
    throw error("Unterminated literal string");
  }

  function parseSingleKeyPart() {
    skipHorizontalWs();
    const ch = peek();
    if (ch === '"') {
      advance(1);
      return parseBasicString(false);
    }
    if (ch === "'") {
      advance(1);
      return parseLiteralString(false);
    }
    let key = "";
    while (pos < len && isBareKeyChar(text[pos])) {
      key += text[pos];
      pos += 1;
    }
    if (!key) {
      throw error("Expected key name");
    }
    return key;
  }

  function parseKeyPath() {
    const parts = [parseSingleKeyPart()];
    while (true) {
      skipHorizontalWs();
      if (peek() === ".") {
        advance(1);
        parts.push(parseSingleKeyPart());
      } else {
        break;
      }
    }
    return parts;
  }

  function parseArray() {
    advance(1); // consume [
    const items = [];
    while (true) {
      skipWsAndComments();
      if (pos >= len) throw error("Unterminated array");
      if (text[pos] === "]") {
        advance(1);
        return items;
      }
      items.push(parseValue());
      skipWsAndComments();
      if (text[pos] === ",") {
        advance(1);
        continue;
      }
      if (text[pos] === "]") {
        advance(1);
        return items;
      }
      throw error("Expected ',' or ']' in array");
    }
  }

  function parseInlineTable() {
    advance(1); // consume {
    const obj = {};
    skipHorizontalWs();
    if (peek() === "}") {
      advance(1);
      return obj;
    }
    while (true) {
      skipHorizontalWs();
      const keyParts = parseKeyPath();
      skipHorizontalWs();
      if (peek() !== "=") throw error("Expected '=' in inline table");
      advance(1);
      skipHorizontalWs();
      const val = parseValue();
      setNestedValue(obj, keyParts, val);
      skipHorizontalWs();
      if (peek() === ",") {
        advance(1);
        continue;
      }
      if (peek() === "}") {
        advance(1);
        return obj;
      }
      throw error("Expected ',' or '}' in inline table");
    }
  }

  function parsePrimitive() {
    let raw = "";
    while (
      pos < len &&
      !/[\s,#\]}]/.test(text[pos])
    ) {
      raw += text[pos];
      pos += 1;
    }
    if (!raw) throw error("Expected value");
    if (raw === "true") return true;
    if (raw === "false") return false;
    if (raw === "inf" || raw === "+inf") return Infinity;
    if (raw === "-inf") return -Infinity;
    if (raw === "nan" || raw === "+nan" || raw === "-nan") return NaN;

    const cleanNum = raw.replaceAll("_", "");
    if (/^[+-]?0x[0-9A-Fa-f]+$/.test(cleanNum)) {
      return parseInt(cleanNum, 16);
    }
    if (/^[+-]?0o[0-7]+$/.test(cleanNum)) {
      return parseInt(cleanNum.replace("0o", ""), 8);
    }
    if (/^[+-]?0b[01]+$/.test(cleanNum)) {
      return parseInt(cleanNum.replace("0b", ""), 2);
    }
    if (/^[+-]?\d+$/.test(cleanNum)) {
      return Number(cleanNum);
    }
    if (/^[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?$/.test(cleanNum)) {
      const n = Number(cleanNum);
      if (!Number.isNaN(n)) return n;
    }
    // Date/time strings kept as ISO strings when present
    if (/^\d{4}-\d{2}-\d{2}/.test(raw)) {
      return raw;
    }
    throw error(`Invalid TOML value '${raw}'`);
  }

  function parseValue() {
    skipHorizontalWs();
    if (pos >= len) throw error("Unexpected end of input while reading value");
    const ch = peek();
    if (ch === '"' && peek(1) === '"' && peek(2) === '"') {
      advance(3);
      return parseBasicString(true);
    }
    if (ch === "'" && peek(1) === "'" && peek(2) === "'") {
      advance(3);
      return parseLiteralString(true);
    }
    if (ch === '"') {
      advance(1);
      return parseBasicString(false);
    }
    if (ch === "'") {
      advance(1);
      return parseLiteralString(false);
    }
    if (ch === "[") {
      return parseArray();
    }
    if (ch === "{") {
      return parseInlineTable();
    }
    return parsePrimitive();
  }

  function setNestedValue(target, keyParts, value) {
    let cursor = target;
    for (let i = 0; i < keyParts.length - 1; i += 1) {
      const k = keyParts[i];
      if (!(k in cursor)) {
        cursor[k] = {};
      } else if (typeof cursor[k] !== "object" || cursor[k] === null || Array.isArray(cursor[k])) {
        throw error(`Key '${k}' is not a table`);
      }
      cursor = cursor[k];
    }
    const finalKey = keyParts[keyParts.length - 1];
    if (finalKey in cursor) {
      throw error(`Duplicate key '${finalKey}'`);
    }
    cursor[finalKey] = value;
  }

  function navigateTable(keyParts, isArrayTable) {
    let cursor = root;
    const prefix = [];
    for (let i = 0; i < keyParts.length - 1; i += 1) {
      const k = keyParts[i];
      prefix.push(k);
      if (!(k in cursor)) {
        cursor[k] = {};
      }
      const next = cursor[k];
      if (Array.isArray(next)) {
        if (next.length === 0) throw error(`Empty array table at '${prefix.join(".")}'`);
        cursor = next[next.length - 1];
      } else if (typeof next === "object" && next !== null) {
        cursor = next;
      } else {
        throw error(`Cannot redefine non-table '${prefix.join(".")}'`);
      }
    }

    const last = keyParts[keyParts.length - 1];
    const fullName = keyParts.join("\u0000");

    if (isArrayTable) {
      if (!(last in cursor)) {
        cursor[last] = [];
        arrayTables.add(fullName);
      } else if (!Array.isArray(cursor[last]) || !arrayTables.has(fullName)) {
        throw error(`Cannot redefine '${keyParts.join(".")}' as an array of tables`);
      }
      const elem = {};
      cursor[last].push(elem);
      return elem;
    }

    if (last in cursor) {
      const existing = cursor[last];
      if (Array.isArray(existing) || typeof existing !== "object" || existing === null) {
        throw error(`Cannot redefine '${keyParts.join(".")}' as a table`);
      }
      if (explicitTables.has(fullName)) {
        throw error(`Duplicate table '[${keyParts.join(".")}]'`);
      }
    } else {
      cursor[last] = {};
    }
    explicitTables.add(fullName);
    return cursor[last];
  }

  while (pos < len) {
    skipWsAndComments();
    if (pos >= len) break;

    if (peek() === "[") {
      if (peek(1) === "[") {
        advance(2);
        const parts = parseKeyPath();
        skipHorizontalWs();
        if (peek() !== "]" || peek(1) !== "]") {
          throw error("Expected ']]' closing array table header");
        }
        advance(2);
        expectLineEndOrEof();
        currentTable = navigateTable(parts, true);
      } else {
        advance(1);
        const parts = parseKeyPath();
        skipHorizontalWs();
        if (peek() !== "]") {
          throw error("Expected ']' closing table header");
        }
        advance(1);
        expectLineEndOrEof();
        currentTable = navigateTable(parts, false);
      }
      continue;
    }

    const keyParts = parseKeyPath();
    skipHorizontalWs();
    if (peek() !== "=") {
      throw error(`Expected '=' after key '${keyParts.join(".")}'`);
    }
    advance(1);
    skipHorizontalWs();
    const val = parseValue();
    expectLineEndOrEof();
    setNestedValue(currentTable, keyParts, val);
  }

  return root;
}
