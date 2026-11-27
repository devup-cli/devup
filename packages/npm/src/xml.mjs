/**
 * Lightweight zero-dependency XML scanner for Maven pom.xml files.
 */

function decodeEntities(text) {
  return text
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&apos;", "'")
    .replaceAll("&amp;", "&");
}

export function extractPomMetadata(xmlText) {
  let cleaned = xmlText.replace(/<!--[\s\S]*?-->/g, "");
  cleaned = cleaned.replace(/<\?[\s\S]*?\?>/g, "");
  cleaned = cleaned.replace(/<!DOCTYPE[\s\S]*?>/gi, "");

  const cdataBlocks = [];
  cleaned = cleaned.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, (_, inner) => {
    const token = `\u0000CDATA${cdataBlocks.length}\u0000`;
    cdataBlocks.push(inner);
    return token;
  });

  const tagRe = /<\/?([A-Za-z_][\w:.-]*)(?:\s[^<>]*?)?\s*(\/?)>/g;
  const stack = [];
  const modules = [];
  const artifactIds = [];
  let sawRoot = false;
  let lastIndex = 0;

  let match;
  while ((match = tagRe.exec(cleaned)) !== null) {
    const rawBetween = cleaned.slice(lastIndex, match.index);
    lastIndex = tagRe.lastIndex;

    const rawTag = match[0];
    const fullName = match[1];
    const localName = fullName.includes(":") ? fullName.split(":").pop() : fullName;
    const isClosing = rawTag.startsWith("</");
    const isSelfClosing = match[2] === "/";

    if (stack.length > 0 && rawBetween) {
      stack[stack.length - 1].text += rawBetween;
    }

    if (isClosing) {
      if (stack.length === 0) {
        throw new Error(`Unexpected closing tag </${fullName}>`);
      }
      const top = stack.pop();
      if (top.localName !== localName) {
        throw new Error(`Mismatched XML tag </${fullName}> for <${top.fullName}>`);
      }
      let text = top.text.replace(/\u0000CDATA(\d+)\u0000/g, (_, idx) => cdataBlocks[Number(idx)]);
      text = decodeEntities(text).trim();
      if (top.localName === "module" && text) {
        modules.push(text);
      } else if (top.localName === "artifactId" && text) {
        artifactIds.push(text);
      }
    } else if (!isSelfClosing) {
      sawRoot = true;
      stack.push({ fullName, localName, text: "" });
    } else {
      sawRoot = true;
    }
  }

  if (!sawRoot) {
    throw new Error("No XML root element found");
  }
  if (stack.length > 0) {
    throw new Error(`Unclosed XML tag <${stack[stack.length - 1].fullName}>`);
  }

  return { modules, artifactIds };
}
