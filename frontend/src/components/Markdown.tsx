import React from "react";

import { type ChatSource, safeHref } from "@/lib/chat";

/**
 * Minimal, safe Markdown for AI answers. It builds React elements only (never raw HTML), so model or
 * news text cannot inject markup or scripts. Links are kept only for absolute http(s) URLs. Citation
 * labels like [S2] become links to the cited source when it has a URL.
 */


const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[S\d{1,3}\]|\[[^\]]+\]\([^)\s]+\)|(?<![\w*])\*[^*\s][^*]*\*(?!\w)|(?<!\w)_[^_\s][^_]*_(?!\w))/g;

function inline(text: string, sources: Map<string, ChatSource>, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const t = m[0];
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    const k = `${key}-${n++}`;
    if (t.startsWith("**")) out.push(<strong key={k}>{t.slice(2, -2)}</strong>);
    else if (t.startsWith("`")) out.push(<code key={k} className="rounded bg-surface-2 px-1 text-[0.9em]">{t.slice(1, -1)}</code>);
    else if (/^\[S\d+\]$/.test(t)) {
      const label = t.slice(1, -1);
      const src = sources.get(label);
      const href = safeHref(src?.url);
      out.push(
        href ? (
          <a key={k} href={href} target="_blank" rel="noopener noreferrer nofollow" title={src?.title ?? label} className="text-accent no-underline">
            [{label}]
          </a>
        ) : (
          <span key={k} title={src?.title ?? label} className="text-accent">
            [{label}]
          </span>
        ),
      );
    } else if (t.startsWith("[")) {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t);
      const href = safeHref(mm?.[2]);
      out.push(
        href ? (
          <a key={k} href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline">
            {mm?.[1]}
          </a>
        ) : (
          <span key={k}>{mm?.[1] ?? t}</span>
        ),
      );
    } else out.push(<em key={k}>{t.slice(1, -1)}</em>);
    last = i + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function cells(row: string): string[] {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

export function Markdown({ text, sources = [] }: { text: string; sources?: ChatSource[] }) {
  const src = new Map(sources.map((s) => [s.label, s]));
  const lines = text.replace(/\r/g, "").split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let b = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${b++}`;
    if (!line.trim()) {
      i++;
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      blocks.push(
        <p key={key} className="mt-2 font-semibold">
          {inline(h[2], src, key)}
        </p>,
      );
      i++;
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      blocks.push(
        <div key={key} className="my-1 overflow-x-auto">
          <table className="text-xs">
            <thead>
              <tr>{head.map((c, j) => <th key={j} className="border-b border-border px-2 py-1 text-left font-medium">{inline(c, src, `${key}h${j}`)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>{r.map((c, j) => <td key={j} className="border-b border-border/50 px-2 py-1">{inline(c, src, `${key}r${ri}c${j}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items: { depth: number; text: string }[] = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        const m = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/.exec(lines[i])!;
        items.push({ depth: Math.min(2, Math.floor(m[1].length / 2)), text: m[3] });
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={key} className={ordered ? "my-1 list-decimal pl-5" : "my-1 list-disc pl-5"}>
          {items.map((it, j) => (
            <li key={j} style={{ marginLeft: it.depth * 14 }}>
              {inline(it.text, src, `${key}i${j}`)}
            </li>
          ))}
        </List>,
      );
      continue;
    }
    if (line.startsWith(">")) {
      const q: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) q.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(
        <blockquote key={key} className="my-1 border-l-2 border-warn/60 pl-2 text-muted">
          {inline(q.join(" "), src, key)}
        </blockquote>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*•]|\d+[.)])\s|>|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    blocks.push(
      <p key={key} className="my-1">
        {inline(para.join(" "), src, key)}
      </p>,
    );
  }
  return <div className="text-sm leading-relaxed">{blocks}</div>;
}
