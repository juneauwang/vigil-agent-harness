import { Fragment, type ReactNode } from "react";

/**
 * Minimal safe Markdown rendering (no react-markdown dependency):
 * bold / inline code / dark code blocks / unordered+ordered lists / paragraphs.
 * Built from pure React elements, no dangerouslySetInnerHTML (React escaping is
 * the last XSS boundary). Code block style follows the vigil dark card look
 * (--vigil-terminal-bg / terminal-text).
 */

const FENCE_RE = /^```(.*)$/;

/** Inline formatting: bold + inline code. */
function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) out.push(<Fragment key={`${keyBase}-t${i++}`}>{text.slice(last, m.index)}</Fragment>);
    if (m[1] !== undefined) {
      out.push(<strong key={`${keyBase}-b${i++}`}>{m[1]}</strong>);
    } else {
      out.push(
        <code key={`${keyBase}-c${i++}`} className="rounded bg-[var(--vigil-muted-bg)] px-1 py-px font-mono text-[0.9em]">
          {m[2]}
        </code>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<Fragment key={`${keyBase}-t${i++}`}>{text.slice(last)}</Fragment>);
  return out;
}

function isListItem(line: string): { ordered: boolean; marker: string; rest: string } | null {
  const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
  if (ul) return { ordered: false, marker: "-", rest: ul[1] };
  const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
  if (ol) return { ordered: true, marker: ol[0].trim().split(/\s+/)[0], rest: ol[1] };
  return null;
}

export function Markdown({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let i = 0;
  let blockKey = 0;

  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const lang = fence[1].trim();
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1; // skip closing fence
      blocks.push(
        <pre
          key={`pre-${blockKey++}`}
          className="scroll-thin my-2 overflow-x-auto rounded-md border border-black/40 p-3 font-mono text-xs leading-relaxed"
          style={{ background: "var(--vigil-terminal-bg)", color: "var(--vigil-terminal-text)" }}
        >
          {lang && (
            <div className="mb-1 text-[10px] uppercase tracking-wide opacity-50">{lang}</div>
          )}
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Merge a run of same-type list items into one <ul>/<ol>
    const item = isListItem(line);
    if (item) {
      const ordered = item.ordered;
      const items: ReactNode[] = [];
      let liKey = 0;
      while (i < lines.length) {
        const cur = isListItem(lines[i]);
        if (!cur || cur.ordered !== ordered) break;
        items.push(<li key={`li-${liKey++}`}>{renderInline(cur.rest, `li-${liKey}`)}</li>);
        i += 1;
      }
      blocks.push(
        ordered ? (
          <ol key={`ol-${blockKey++}`} className="my-1 list-decimal space-y-0.5 pl-5">
            {items}
          </ol>
        ) : (
          <ul key={`ul-${blockKey++}`} className="my-1 list-disc space-y-0.5 pl-5">
            {items}
          </ul>
        ),
      );
      continue;
    }

    // Plain paragraph
    const para: string[] = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() !== "" && !FENCE_RE.test(lines[i]) && !isListItem(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={`p-${blockKey++}`} className="my-1 whitespace-pre-wrap break-words">
        {renderInline(para.join("\n"), `p-${blockKey}`)}
      </p>,
    );
  }

  return <div className="text-sm leading-relaxed">{blocks}</div>;
}

