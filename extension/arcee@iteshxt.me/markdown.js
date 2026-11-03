// Arcee's replies, written in Markdown by the models, as Pango markup for the Shell's
// labels (the panel and the pill). The same converter as arcee/markdown.py, which a
// test keeps in step with this one. The Shell's labels cannot open links, so links
// are underlined. No imports: it also runs under node, for that test.

const FENCE = /^\s*(```|~~~)/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const HEADING = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/;
const TABLE = /^\s*\|(.*)\|\s*$/;
const TABLE_RULE = /^[\s|:-]+$/;
const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const NUMBERED = /^(\s*)(\d+)[.)]\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const CODE_SPAN = /`([^`]+)`/g;
const LINK = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
const BOLD = /\*\*(.+?)\*\*|__(.+?)__/g;
const STRIKE = /~~(.+?)~~/g;
const ITALIC = /(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])/g;

function escape(text) {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

function words(text, links) {
    text = escape(text);
    text = text.replace(LINK, links ? '<a href="$2">$1</a>' : '<u>$1</u>');
    text = text.replace(BOLD, (_m, a, b) => `<b>${a ?? b}</b>`);
    text = text.replace(STRIKE, '<s>$1</s>');
    return text.replace(ITALIC, (_m, a, b) => `<i>${a ?? b}</i>`);
}

function inline(text, links) {
    let out = '';
    let last = 0;
    for (const match of text.matchAll(CODE_SPAN)) {
        out += words(text.slice(last, match.index), links);
        out += `<tt>${escape(match[1])}</tt>`;
        last = match.index + match[0].length;
    }
    return out + words(text.slice(last), links);
}

export function toPango(text, {links = false} = {}) {
    const out = [];
    let code = false;
    for (const raw of (text ?? '').replace(/\r\n/g, '\n').split('\n')) {
        if (FENCE.test(raw)) {
            code = !code;
            continue;
        }
        if (code) {
            out.push(`<tt>${escape(raw.trimEnd())}</tt>`);
            continue;
        }
        const line = raw.trimEnd();
        if (!line.trim() || RULE.test(line)) {
            if (out.length && out[out.length - 1])
                out.push('');
            continue;
        }
        let m;
        if ((m = HEADING.exec(line))) {
            out.push(`<b>${inline(m[2], links)}</b>`);
        } else if ((m = TABLE.exec(line))) {
            if (TABLE_RULE.test(m[1]))
                continue;
            out.push(m[1].split('|').map(c => inline(c.trim(), links)).filter(Boolean).join('  ·  '));
        } else if ((m = BULLET.exec(line))) {
            out.push(`${'    '.repeat(Math.floor(m[1].length / 2))}•  ${inline(m[2], links)}`);
        } else if ((m = NUMBERED.exec(line))) {
            out.push(`${'    '.repeat(Math.floor(m[1].length / 2))}${m[2]}.  ${inline(m[3], links)}`);
        } else if ((m = QUOTE.exec(line))) {
            out.push(`<i>${inline(m[1], links)}</i>`);
        } else {
            out.push(inline(line.trim(), links));
        }
    }
    while (out.length && !out[out.length - 1])
        out.pop();
    return out.join('\n');
}

// Markdown as plain text, for places that show no formatting.
export function plain(text) {
    return toPango(text).replace(/<[^>]+>/g, '').replace(/&quot;/g, '"').replace(/&#x27;/g, "'")
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
